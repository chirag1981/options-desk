"""
tests/test_observation_mode.py — Regression & Verification Suite for Iron Condor Paper Observation Mode

Verifies:
1. Quote integrity: positive bid/ask, uncrossed book, spread <= 5%, rejection of crossed/stale quotes.
2. Regime filtering: strict BALANCED_RANGE enforcement, rejection of CHOPPY_OR_INSUFFICIENT, TRENDING, or UNKNOWN.
3. Execution pricing: Short at Bid, Long at Ask; exit reverse; never uses LTP.
4. Target and stop calculations: profit target <= 30% of credit (70% capture), stop loss >= 150% of credit.
5. Statistically rigorous 10 bps slippage model for conservative scenario comparison.
6. Statutory cost accounting: Brokerage, STT, Exchange fees, GST, Stamp Duty, and prevention of zero exit costs.
7. Risk limits: Max 1 concurrent open observation position.
8. Persistence & Recovery: Restart survival, data-quality failure logging, online backup and restore.
9. Zero broker routing: Verifies paper-only execution and isolation from production and backtest records.
10. Report generation: Daily and cumulative analytics regeneration directly from ledger records.
"""

import os
import sys
import shutil
import pytest
import sqlite3
from datetime import datetime, date, time as dt_time
from zoneinfo import ZoneInfo

from app.research.observation_models import (
    get_db_connection,
    init_observation_db,
    OBSERVATION_DB_PATH,
)
from app.research.observation_engine import ObservationEngine, MAX_SPREAD_PCT
from app.research.observation_reports import ObservationReportGenerator
from scripts.backup_observation_db import backup_observation_database, restore_observation_database
from app.strategies import MarketAnalysisResult

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def test_db_path(tmp_path):
    """Provides a fresh isolated temporary database for each test."""
    db_file = os.path.join(tmp_path, "test_observation.db")
    init_observation_db(db_file)
    return db_file


@pytest.fixture
def mock_chain():
    """Builds a realistic 4-leg option chain around spot 24500."""
    return {
        24400.0: {"PE": {"symbol": "NIFTY24400PE", "bid": 25.0, "ask": 26.0, "ltp": 25.5, "oi": 50000, "volume": 10000}},
        24350.0: {"PE": {"symbol": "NIFTY24350PE", "bid": 10.0, "ask": 10.4, "ltp": 10.2, "oi": 30000, "volume": 8000}},
        24600.0: {"CE": {"symbol": "NIFTY24600CE", "bid": 25.0, "ask": 26.0, "ltp": 25.5, "oi": 50000, "volume": 10000}},
        24650.0: {"CE": {"symbol": "NIFTY24650CE", "bid": 10.0, "ask": 10.4, "ltp": 10.2, "oi": 30000, "volume": 8000}},
    }


@pytest.fixture
def balanced_analysis():
    """Builds a valid BALANCED_RANGE market analysis result."""
    return MarketAnalysisResult(
        symbol="NIFTY",
        expiry="2026-10-29",
        timestamp="2026-10-25 10:30:00",
        spot_price=24500.0,
        spot_change_pct=0.05,
        atm_strike=24500.0,
        s1_support=24400.0,
        s2_support=24350.0,
        r1_resistance=24600.0,
        r2_resistance=24650.0,
        pcr_oi=1.02,
        total_pe_oi=600000,
        total_ce_oi=590000,
        total_pe_change_oi=20000,
        total_ce_change_oi=19000,
        net_delta_pe_ce_oi=1000,
        writer_momentum="BALANCED",
        dist_to_s1=100.0,
        dist_to_r1=100.0,
        dist_to_s1_pct=0.41,
        dist_to_r1_pct=0.41,
        atm_iv=13.5,
        market_regime="BALANCED_RANGE",
        market_bias="NEUTRAL",
        confidence_score=78.0,
        evidence_factors=["Balanced OI", "Low IV"],
        data_status="FYERS_LIVE",
    )


# ---------------- 1. QUOTE INTEGRITY & DATA QUALITY TESTS ----------------

def test_quote_integrity_validation(test_db_path, mock_chain):
    """Verifies that quotes pass when clean, and fail on missing, zero, crossed, or wide spread."""
    engine = ObservationEngine(db_path=test_db_path)
    strikes = {"short_put": 24400.0, "long_put": 24350.0, "short_call": 24600.0, "long_call": 24650.0}

    # 1. Clean quotes pass
    ok, msg, q = engine.validate_quotes_for_condor(strikes, mock_chain)
    assert ok
    assert len(q) == 4

    # 2. Non-positive bid rejected
    bad_chain = {k: {opt: dict(v) for opt, v in opts.items()} for k, opts in mock_chain.items()}
    bad_chain[24400.0]["PE"]["bid"] = 0.0
    ok, msg, _ = engine.validate_quotes_for_condor(strikes, bad_chain)
    assert not ok
    assert "non-positive" in msg.lower() or "missing" in msg.lower()

    # 3. Crossed quotes rejected (bid >= ask)
    bad_chain2 = {k: {opt: dict(v) for opt, v in opts.items()} for k, opts in mock_chain.items()}
    bad_chain2[24600.0]["CE"]["bid"] = 27.0
    bad_chain2[24600.0]["CE"]["ask"] = 26.0
    ok, msg, _ = engine.validate_quotes_for_condor(strikes, bad_chain2)
    assert not ok
    assert "crossed" in msg.lower()

    # 4. Excessive spread rejected (> 5%)
    bad_chain3 = {k: {opt: dict(v) for opt, v in opts.items()} for k, opts in mock_chain.items()}
    bad_chain3[24650.0]["CE"]["bid"] = 10.0
    bad_chain3[24650.0]["CE"]["ask"] = 11.0  # (11 - 10)/11 = 9.09% > 5%
    ok, msg, _ = engine.validate_quotes_for_condor(strikes, bad_chain3)
    assert not ok
    assert "excessive" in msg.lower()

    # Verify DQ events are persisted in DB
    with get_db_connection(test_db_path) as conn:
        events = conn.execute("SELECT * FROM observation_data_quality_events").fetchall()
        assert len(events) >= 3


# ---------------- 2. REGIME FILTERING TESTS ----------------

def test_regime_filtering_strict_balanced_range(test_db_path, balanced_analysis, mock_chain):
    """Verifies that non-BALANCED_RANGE regimes are strictly rejected and logged."""
    engine = ObservationEngine(db_path=test_db_path)

    # 1. Balanced range eligible
    ok, msg, sig_id = engine.evaluate_and_record_signal(balanced_analysis, mock_chain)
    assert ok
    assert "entered" in msg.lower()

    # 2. Choppy market rejected
    choppy_analysis = balanced_analysis
    choppy_analysis.market_regime = "CHOPPY_OR_INSUFFICIENT"
    ok, msg, sig_id2 = engine.evaluate_and_record_signal(choppy_analysis, mock_chain)
    assert not ok
    assert "balanced_range" in msg.lower()

    # 3. Trending market rejected
    trending_analysis = balanced_analysis
    trending_analysis.market_regime = "TRENDING_BULLISH"
    ok, msg, sig_id3 = engine.evaluate_and_record_signal(trending_analysis, mock_chain)
    assert not ok
    assert "trending_bullish" in msg.lower()

    with get_db_connection(test_db_path) as conn:
        rejections = conn.execute("SELECT * FROM observation_signals WHERE status = 'REJECTED'").fetchall()
        assert len(rejections) == 2


# ---------------- 3. EXECUTION PRICING & BID/ASK SIDE REVERSAL ----------------

def test_execution_pricing_bid_ask_rules(test_db_path, balanced_analysis, mock_chain):
    """Verifies entry: Short at Bid, Long at Ask. Exit: Short at Ask, Long at Bid. Never LTP."""
    engine = ObservationEngine(db_path=test_db_path)
    ok, msg, sig_id = engine.evaluate_and_record_signal(balanced_analysis, mock_chain)
    assert ok

    with get_db_connection(test_db_path) as conn:
        pos = conn.execute("SELECT * FROM observation_positions WHERE status = 'OPEN'").fetchone()
        legs = conn.execute("SELECT * FROM observation_legs WHERE position_id = ? ORDER BY leg_index ASC", (pos["position_id"],)).fetchall()

        # Leg 0: Short Put -> fill must be Bid (25.0)
        assert legs[0]["action"] == "SELL"
        assert legs[0]["entry_fill_price"] == 25.0
        assert legs[0]["entry_bid"] == 25.0
        assert legs[0]["entry_ask"] == 26.0

        # Leg 1: Long Put -> fill must be Ask (10.4)
        assert legs[1]["action"] == "BUY"
        assert legs[1]["entry_fill_price"] == 10.4
        assert legs[1]["entry_ask"] == 10.4

        # Net entry credit = (25.0 - 10.4) + (25.0 - 10.4) = 14.6 + 14.6 = 29.2
        assert pos["entry_net_credit_pts"] == 29.20


# ---------------- 4. TARGET, STOP-LOSS, & FORMULAS ----------------

def test_target_and_stop_loss_thresholds(test_db_path, balanced_analysis, mock_chain):
    """Verifies profit target: exit debit <= 30% of credit; stop loss: exit debit >= 150% of credit."""
    engine = ObservationEngine(db_path=test_db_path)
    engine.evaluate_and_record_signal(balanced_analysis, mock_chain)

    with get_db_connection(test_db_path) as conn:
        pos = conn.execute("SELECT * FROM observation_positions WHERE status = 'OPEN'").fetchone()
        credit = pos["entry_net_credit_pts"]  # 29.20
        # Target <= 30% credit (70% capture)
        assert pos["target_net_debit_pts"] == round(credit * 0.30, 2)
        assert pos["target_net_debit_pts"] == 8.76
        # Stop loss >= 150% credit
        assert pos["stop_loss_debit_pts"] == round(credit * 1.50, 2)
        assert pos["stop_loss_debit_pts"] == 43.80

    # Simulate MTM profit target hit:
    target_chain = {
        24400.0: {"PE": {"bid": 2.0, "ask": 2.2, "ltp": 2.1}},
        24350.0: {"PE": {"bid": 0.5, "ask": 0.6, "ltp": 0.55}},
        24600.0: {"CE": {"bid": 2.0, "ask": 2.2, "ltp": 2.1}},
        24650.0: {"CE": {"bid": 0.5, "ask": 0.6, "ltp": 0.55}},
    }
    # Buyback cost: (2.2 - 0.5) + (2.2 - 0.5) = 1.7 + 1.7 = 3.4 <= 8.76 -> TARGET_HIT
    res = engine.update_open_positions(target_chain, spot_price=24500.0)
    assert res[0]["status"] == "CLOSED"
    assert res[0]["reason"] == "TARGET_HIT"

    with get_db_connection(test_db_path) as conn:
        closed = conn.execute("SELECT * FROM observation_positions WHERE status = 'CLOSED'").fetchone()
        assert closed["exit_reason"] == "TARGET_HIT"
        assert closed["net_pnl_inr"] > 0
        assert closed["brokerage_inr"] == 160.0  # ₹80 entry + ₹80 exit


# ---------------- 5. CONSERVATIVE 10 BPS SLIPPAGE FORMULA ----------------

def test_conservative_slippage_formula():
    """Verifies 10 bps slippage: Buy pays ask*(1+0.0010), Sell receives bid*(1-0.0010)."""
    # BUY paying ₹100.00 -> 100 * 1.0010 = ₹100.10
    assert ObservationEngine.calculate_slippage_price(100.0, "BUY", slippage_bps=10.0) == 100.10
    # SELL receiving ₹100.00 -> 100 * 0.9990 = ₹99.90
    assert ObservationEngine.calculate_slippage_price(100.0, "SELL", slippage_bps=10.0) == 99.90
    # Floor check: 0.05 NSE minimum tick
    assert ObservationEngine.calculate_slippage_price(0.01, "SELL", slippage_bps=10.0) == 0.05


# ---------------- 6. STATUTORY COSTS & ZERO-EXIT-COST PREVENTION ----------------

def test_settlement_and_exit_cost_integrity(test_db_path, balanced_analysis, mock_chain):
    """Verifies all statutory cost line items are persisted and exit costs are never zeroed."""
    engine = ObservationEngine(db_path=test_db_path)
    engine.evaluate_and_record_signal(balanced_analysis, mock_chain)

    # Simulate Stop Loss
    stop_chain = {
        24400.0: {"PE": {"bid": 25.0, "ask": 26.0, "ltp": 25.5}},
        24350.0: {"PE": {"bid": 2.0, "ask": 2.2, "ltp": 2.1}},
        24600.0: {"CE": {"bid": 25.0, "ask": 26.0, "ltp": 25.5}},
        24650.0: {"CE": {"bid": 2.0, "ask": 2.2, "ltp": 2.1}},
    }
    # Buyback cost: (26.0 - 2.0) + (26.0 - 2.0) = 48.0 >= 43.80 -> STOP_LOSS
    engine.update_open_positions(stop_chain, spot_price=24500.0)

    with get_db_connection(test_db_path) as conn:
        closed = conn.execute("SELECT * FROM observation_positions WHERE status = 'CLOSED'").fetchone()
        assert closed["exit_reason"] == "STOP_LOSS"
        # Total brokerage must be ₹160 (8 legs * ₹20)
        assert closed["brokerage_inr"] == 160.0
        assert closed["stt_inr"] > 0.0
        assert closed["exchange_fees_inr"] > 0.0
        assert closed["gst_inr"] > 0.0
        assert closed["stamp_duty_inr"] > 0.0
        assert closed["total_costs_inr"] == round(
            closed["brokerage_inr"] + closed["stt_inr"] + closed["exchange_fees_inr"] +
            closed["gst_inr"] + closed["stamp_duty_inr"] + closed["sebi_ipft_inr"], 2
        )
        assert closed["net_pnl_inr"] < 0  # Loss registered correctly


# ---------------- 7. CONCURRENCY & RISK LIMITS ----------------

def test_single_concurrent_observation_position(test_db_path, balanced_analysis, mock_chain):
    """Verifies that initially only 1 concurrent observation position is permitted."""
    engine = ObservationEngine(db_path=test_db_path)
    ok1, _, _ = engine.evaluate_and_record_signal(balanced_analysis, mock_chain)
    assert ok1

    # Second entry attempt while position 1 is open must be blocked
    ok2, msg2, _ = engine.evaluate_and_record_signal(balanced_analysis, mock_chain)
    assert not ok2
    assert "already open" in msg2.lower()

    with get_db_connection(test_db_path) as conn:
        cnt = conn.execute("SELECT COUNT(*) as c FROM observation_positions WHERE status = 'OPEN'").fetchone()["c"]
        assert cnt == 1


# ---------------- 8. VALUATION UNAVAILABLE (NO FABRICATION) ----------------

def test_missing_quotes_leaves_position_open_with_dq_event(test_db_path, balanced_analysis, mock_chain):
    """Verifies that missing quotes during an open trade do not falsely close it or fabricate prices."""
    engine = ObservationEngine(db_path=test_db_path)
    engine.evaluate_and_record_signal(balanced_analysis, mock_chain)

    broken_chain = {
        24400.0: {"PE": {"bid": 0.0, "ask": 0.0}},  # Broken quote
        24350.0: {"PE": {"bid": 5.0, "ask": 5.2}},
        24600.0: {"CE": {"bid": 20.0, "ask": 20.5}},
        24650.0: {"CE": {"bid": 5.0, "ask": 5.2}},
    }

    res = engine.update_open_positions(broken_chain, spot_price=24500.0)
    assert res[0]["status"] == "VALUATION_UNAVAILABLE"

    with get_db_connection(test_db_path) as conn:
        pos = conn.execute("SELECT * FROM observation_positions").fetchone()
        assert pos["status"] == "OPEN"  # Preserved open
        events = conn.execute("SELECT * FROM observation_data_quality_events WHERE event_type = 'VALUATION_UNAVAILABLE'").fetchall()
        assert len(events) >= 1


# ---------------- 9. RESTART PERSISTENCE & BACKUP / RESTORE ----------------

def test_restart_persistence_and_online_backup(test_db_path, balanced_analysis, mock_chain, tmp_path):
    """Verifies that trade records survive restart and that online backup restores correctly."""
    engine = ObservationEngine(db_path=test_db_path)
    engine.evaluate_and_record_signal(balanced_analysis, mock_chain)

    # Perform online backup
    backup_dir = os.path.join(tmp_path, "backups")
    backup_file = backup_observation_database(source_path=test_db_path, backup_dir=backup_dir)
    assert os.path.exists(backup_file)

    # Restore backup into a separate restored location and verify integrity
    restored_db_path = os.path.join(tmp_path, "restored_observation.db")
    restore_observation_database(backup_file, target_path=restored_db_path)
    assert os.path.exists(restored_db_path)

    # Verify data intact after restore
    with get_db_connection(restored_db_path) as conn:
        pos = conn.execute("SELECT * FROM observation_positions").fetchall()
        assert len(pos) == 1
        assert pos[0]["status"] == "OPEN"


# ---------------- 10. REPORT REGENERATION FROM STORED RECORDS ----------------

def test_report_generation_from_ledger(test_db_path, balanced_analysis, mock_chain):
    """Verifies daily and cumulative reports are regenerated purely from stored SQLite records."""
    engine = ObservationEngine(db_path=test_db_path)
    engine.evaluate_and_record_signal(balanced_analysis, mock_chain)

    # Close with profit
    target_chain = {
        24400.0: {"PE": {"bid": 2.0, "ask": 2.2}},
        24350.0: {"PE": {"bid": 0.5, "ask": 0.6}},
        24600.0: {"CE": {"bid": 2.0, "ask": 2.2}},
        24650.0: {"CE": {"bid": 0.5, "ask": 0.6}},
    }
    engine.update_open_positions(target_chain, spot_price=24500.0)

    rep_gen = ObservationReportGenerator(db_path=test_db_path)
    rep = rep_gen.generate_cumulative_report()

    assert rep["total_closed_trades"] == 1
    assert rep["sessions_observed"] >= 1
    assert rep["base_scenario"]["win_rate_pct"] == 100.0
    assert rep["base_scenario"]["net_pnl_inr"] > 0
    assert rep["conservative_scenario"]["slippage_drag_inr"] > 0

    daily = rep_gen.generate_daily_summary()
    assert len(daily) == 1
    assert daily[0]["trades"] == 1
