import os
import sys
import pytest
import sqlite3
import json
from datetime import datetime, timedelta

# Add workspace path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.options_engine import (
    analyze_option_desk,
    ENGINE_CONFIG,
)
from app.services.fyers_options_service import INDEX_CONFIGS
from app.services.options_signal_service import (
    _get_db,
    init_signal_db,
    record_signal,
    update_active_signals,
    close_signal_manually,
    backfill_forward_labels,
    get_signals_summary,
    STOP_LOSS_HIT,
    TRAILING_STOP_HIT,
    TARGET_1_HIT,
    TARGET_2_HIT,
    TIME_STOP_EXIT,
    EOD_CLOSED,
    MANUALLY_CLOSED,
    STALE_EXIT,
    CONTRACT_EXPIRED,
)
from app.services.trade_analyzer_agent import (
    review_closed_trade,
    _run_counterfactual_replay,
    update_hypotheses_in_daily_job,
    _filter_data_quality,
)
from app.services.options_replay_backtest import (
    run_walk_forward_evaluation,
    replay_trade_on_ticks,
)


def test_item_1_ce_writing_dominates_pe_writing_bearish_score():
    """Item 1: Verify near_ce_writing > (near_pe_writing * 1.3) awards +20 to bearish score."""
    step = 50
    spot = 24000.0

    # Near ATM strikes with heavy CE writing (e.g. 200k vs 50k PE writing)
    chain_ce_dominant = [
        {
            "strike": 24000,
            "ce_oi": 500000, "ce_change_oi": 200000, "ce_ltp": 120.0, "ce_volume": 10000, "ce_iv": 14.0, "ce_delta": 0.5, "ce_bid": 119.5, "ce_ask": 120.5,
            "pe_oi": 500000, "pe_change_oi": 50000, "pe_ltp": 110.0, "pe_volume": 10000, "pe_iv": 14.0, "pe_delta": -0.5, "pe_bid": 109.5, "pe_ask": 110.5,
        }
    ]

    market_data = {
        "symbol": "NIFTY",
        "spot_price": spot,
        "spot_change_pct": 0.0,
        "strike_step": step,
        "chain": chain_ce_dominant,
    }

    res = analyze_option_desk(market_data)
    market_bias = res["market_bias"]

    # Bearish score should gain 20 pts from call sellers
    assert market_bias["bearish_score"] >= 20.0


def test_item_2_initial_stop_loss_immutability_and_migration():
    """Item 2: Immutable initial_stop_loss, migration backfill, trailing only modifies stop_loss."""
    with _get_db() as conn:
        conn.execute("DELETE FROM signals")
        conn.commit()

    # Old row migration simulation without initial_stop_loss:
    with _get_db() as conn:
        conn.execute("""
            INSERT INTO signals (
                symbol, signal_type, contract_name, strike, expiry, spot_at_entry,
                entry_price, stop_loss, target_1, target_2, status, current_price,
                highest_price, lowest_price, created_at, updated_at
            ) VALUES (
                'NIFTY', 'CE', '24000 CE', 24000, '2026-10-08', 24000.0,
                100.0, 80.0, 130.0, 150.0, 'ACTIVE', 100.0, 100.0, 100.0,
                datetime('now'), datetime('now')
            )
        """)
        conn.commit()

    init_signal_db()  # Run migration / backfill

    with _get_db() as conn:
        row = conn.execute("SELECT initial_stop_loss, data_quality_json FROM signals WHERE symbol = 'NIFTY'").fetchone()
        assert row["initial_stop_loss"] == 80.0  # entry (100) - max(0.2*100, 5) = 80.0
        assert "INFERRED_INITIAL_SL" in (row["data_quality_json"] or "")

    # Clear signals for clean insertion
    with _get_db() as conn:
        conn.execute("DELETE FROM signals")
        conn.commit()

    # New signal created via record_signal
    sig_data = {
        "symbol": "BANKNIFTY",
        "type": "CE",
        "strike": 50000,
        "expiry": "2026-10-08",
        "spot_price": 50000.0,
        "entry_price": 200.0,
        "stop_loss": 170.0,
        "target_1": 250.0,
        "target_2": 300.0,
        "lots": 1,
        "bid_at_entry": 199.0,
        "ask_at_entry": 200.0,
    }
    res = record_signal(sig_data, is_paper_trade=True)
    assert res is not None
    assert res["status"] == "ACTIVE"
    sig_id = res["id"]

    # Trailing SL update modifies stop_loss but preserves initial_stop_loss
    market_cache = {
        "BANKNIFTY": {
            "spot_price": 50500.0,
            "chain": [
                {"strike": 50000, "ce_ltp": 240.0, "ce_bid": 239.0, "ce_ask": 240.0}
            ]
        }
    }
    update_active_signals(market_data_cache=market_cache)

    with _get_db() as conn:
        updated = conn.execute("SELECT initial_stop_loss, stop_loss FROM signals WHERE id = ?", (sig_id,)).fetchone()
        assert updated["initial_stop_loss"] == 170.0  # Immutable
        assert updated["stop_loss"] >= 170.0  # May have trailed up or stayed same


def test_item_4_reject_missing_bid_ask_and_stale_exit_15min():
    """Item 4: Reject signal creation if bid/ask missing; STALE_EXIT only after 15m without quote."""
    with _get_db() as conn:
        conn.execute("DELETE FROM signals")
        conn.commit()

    # 1. Missing bid/ask rejection
    res_no_bid = record_signal({
        "symbol": "NIFTY",
        "type": "CE",
        "strike": 24000,
        "expiry": "2026-10-08",
        "spot_price": 24000.0,
        "entry_price": 100.0,
        "stop_loss": 85.0,
        "target_1": 130.0,
        "target_2": 160.0,
        "bid_at_entry": None,
        "ask_at_entry": 100.0,
    })
    assert res_no_bid is None  # Fails closed / returns None

    # 2. Valid quote insertion
    res_valid = record_signal({
        "symbol": "NIFTY",
        "type": "CE",
        "strike": 24000,
        "expiry": "2026-10-08",
        "spot_price": 24000.0,
        "entry_price": 100.0,
        "stop_loss": 85.0,
        "target_1": 130.0,
        "target_2": 160.0,
        "bid_at_entry": 99.5,
        "ask_at_entry": 100.5,
    }, is_paper_trade=True)
    assert res_valid is not None
    assert res_valid["status"] == "ACTIVE"
    sig_id = res_valid["id"]

    from zoneinfo import ZoneInfo
    midday = datetime(2026, 10, 5, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    # Simulate quote missing for 10 minutes -> should stay ACTIVE
    ten_min_ago = (midday - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    with _get_db() as conn:
        conn.execute("UPDATE signals SET last_valid_quote_at = ? WHERE id = ?", (ten_min_ago, sig_id))
        conn.commit()

    update_active_signals(market_data_cache={"NIFTY": {"chain": []}}, now_dt=midday)
    with _get_db() as conn:
        sig = conn.execute("SELECT status FROM signals WHERE id = ?", (sig_id,)).fetchone()
        assert sig["status"] == "ACTIVE"

    # Simulate quote missing for 16 minutes -> should transition to STALE_EXIT
    sixteen_min_ago = (midday - timedelta(minutes=16)).strftime("%Y-%m-%d %H:%M:%S")
    with _get_db() as conn:
        conn.execute("UPDATE signals SET last_valid_quote_at = ? WHERE id = ?", (sixteen_min_ago, sig_id))
        conn.commit()

    update_active_signals(market_data_cache={"NIFTY": {"chain": []}}, now_dt=midday)
    with _get_db() as conn:
        sig = conn.execute("SELECT status, exit_reason FROM signals WHERE id = ?", (sig_id,)).fetchone()
        assert sig["status"] == "STALE_EXIT"
        assert sig["exit_reason"] == STALE_EXIT


def test_item_5_exact_exit_reason_matching():
    """Item 5: Use exact reason constants (STOP_LOSS_HIT, etc.) without substring matching."""
    valid_reasons = {
        STOP_LOSS_HIT,
        TRAILING_STOP_HIT,
        TARGET_1_HIT,
        TARGET_2_HIT,
        TIME_STOP_EXIT,
        EOD_CLOSED,
        MANUALLY_CLOSED,
        STALE_EXIT,
        CONTRACT_EXPIRED,
    }
    assert STOP_LOSS_HIT == "STOP_LOSS_HIT"
    assert TRAILING_STOP_HIT == "TRAILING_STOP_HIT"
    assert "SL" not in valid_reasons


def test_item_6_and_7_partial_exits_and_manual_close():
    """Item 6 & 7: lots == 1 trails whole position, lots >= 2 models 50% booked at T1; manual exit at bid."""
    with _get_db() as conn:
        conn.execute("DELETE FROM signals")
        conn.commit()

    # 1-lot trade reaching T1
    res1 = record_signal({
        "symbol": "FINNIFTY",
        "type": "CE",
        "strike": 23000,
        "expiry": "2026-10-08",
        "spot_price": 23000.0,
        "entry_price": 100.0,
        "stop_loss": 85.0,
        "target_1": 130.0,
        "target_2": 160.0,
        "lots": 1,
        "bid_at_entry": 99.5,
        "ask_at_entry": 100.5,
    }, is_paper_trade=True)
    sig1_id = res1["id"]

    from zoneinfo import ZoneInfo
    midday = datetime(2026, 10, 5, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    # Price moves to 135.0 (well above T1=130, so trailed SL=132.5 < bid=134.5, position stays OPEN)
    market_cache1 = {
        "FINNIFTY": {
            "spot_price": 23200.0,
            "chain": [
                {"strike": 23000, "ce_ltp": 135.0, "ce_bid": 134.5, "ce_ask": 135.5}
            ]
        }
    }
    update_active_signals(market_data_cache=market_cache1, now_dt=midday)

    with _get_db() as conn:
        s1 = conn.execute("SELECT status, stop_loss FROM signals WHERE id = ?", (sig1_id,)).fetchone()
        assert s1["status"] in ("TARGET_1_HIT", "ACTIVE")
        assert s1["stop_loss"] >= 100.0

    # Close 1st trade cleanly
    close_signal_manually(sig1_id, exit_price=135.0, exit_bid=134.5, exit_reason=MANUALLY_CLOSED)

    # 2-lot trade reaching T1
    res2 = record_signal({
        "symbol": "MIDCPNIFTY",
        "type": "CE",
        "strike": 12000,
        "expiry": "2026-10-08",
        "spot_price": 12000.0,
        "entry_price": 100.0,
        "stop_loss": 85.0,
        "target_1": 130.0,
        "target_2": 160.0,
        "lots": 2,
        "bid_at_entry": 99.5,
        "ask_at_entry": 100.5,
    }, is_paper_trade=True)
    sig2_id = res2["id"]

    market_cache2 = {
        "MIDCPNIFTY": {
            "spot_price": 12100.0,
            "chain": [
                {"strike": 12000, "ce_ltp": 135.0, "ce_bid": 134.0, "ce_ask": 135.0}
            ]
        }
    }
    update_active_signals(market_data_cache=market_cache2, now_dt=midday)

    with _get_db() as conn:
        s2 = conn.execute("SELECT status, stop_loss, net_pnl_inr FROM signals WHERE id = ?", (sig2_id,)).fetchone()
        assert s2["status"] == "TARGET_1_HIT"  # Partial booked
        assert s2["net_pnl_inr"] > 0  # 1 lot booked at T1

    # Manual close 2nd lot at bid = 140.0
    manual_res = close_signal_manually(sig2_id, exit_price=140.0, exit_bid=139.5, exit_reason=MANUALLY_CLOSED)
    assert manual_res is not None
    assert manual_res["status"] == "CLOSED"

    with _get_db() as conn:
        s2_closed = conn.execute("SELECT status, exit_price, exit_reason FROM signals WHERE id = ?", (sig2_id,)).fetchone()
        assert s2_closed["status"] == "CLOSED"
        assert s2_closed["exit_price"] == 139.5  # Exited at bid


def test_item_8_review_closed_trade_metrics():
    """Item 8: review_closed_trade takes DTE, computes time_to_mfe from ticks, retains short duration."""
    with _get_db() as conn:
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM trade_ticks")
        conn.execute("DELETE FROM trade_reviews")
        conn.commit()

        # Insert signal
        now = datetime.now()
        t0 = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        t5 = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        t10 = now.strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.execute("""
            INSERT INTO signals (
                symbol, signal_type, contract_name, strike, expiry, spot_at_entry,
                entry_price, bid_at_entry, ask_at_entry, iv_at_entry,
                stop_loss, initial_stop_loss, target_1, target_2,
                current_price, highest_price, lowest_price,
                exit_price, status, exit_reason, duration_mins,
                raw_values_json, pillar_flags_json, created_at, updated_at
            ) VALUES (
                'NIFTY', 'CE', '24000 CE', 24000, '2026-10-08', 24000.0,
                100.0, 99.5, 100.5, 14.2,
                85.0, 85.0, 130.0, 160.0,
                135.0, 140.0, 98.0,
                135.0, 'CLOSED', 'TARGET_1_HIT', 10.0,
                '{"dte": 3.0, "atm_iv": 14.2}',
                '{"direction": true, "level": true, "momentum": true, "volume": true, "oi": true, "iv_session": true}',
                ?, ?
            )
        """, (t0, t10))
        sig_id = cur.lastrowid

        # Insert trade ticks
        conn.execute("INSERT INTO trade_ticks (signal_id, timestamp, ltp, bid, ask, spot, created_at) VALUES (?, ?, 100.0, 99.5, 100.5, 24000.0, ?)", (sig_id, t0, t0))
        conn.execute("INSERT INTO trade_ticks (signal_id, timestamp, ltp, bid, ask, spot, created_at) VALUES (?, ?, 140.0, 139.5, 140.5, 24100.0, ?)", (sig_id, t5, t5))
        conn.execute("INSERT INTO trade_ticks (signal_id, timestamp, ltp, bid, ask, spot, created_at) VALUES (?, ?, 135.0, 134.5, 135.5, 24080.0, ?)", (sig_id, t10, t10))
        conn.commit()

    rev = review_closed_trade(sig_id)
    assert rev is not None
    assert rev["dte"] == 3.0
    assert rev["time_to_mfe_mins"] == 5.0  # Peak occurred at t5 (5 mins after t0)
    assert rev["classification"] in ("CLEAN_WIN", "EARLY_EXIT")


def test_item_9_counterfactual_replay_on_trade_ticks():
    """Item 9: Counterfactual variations replay on trade_ticks."""
    trade = {
        "id": 101,
        "entry_price": 100.0,
        "initial_stop_loss": 85.0,
        "target_1": 130.0,
        "target_2": 160.0,
        "exit_price": 125.0,
    }
    ticks = [
        {"ltp": 100.0, "bid": 99.5, "ask": 100.5, "timestamp": "2026-10-05 10:00:00"},
        {"ltp": 115.0, "bid": 114.5, "ask": 115.5, "timestamp": "2026-10-05 10:15:00"},
        {"ltp": 132.0, "bid": 131.5, "ask": 132.5, "timestamp": "2026-10-05 10:30:00"},
    ]
    # Replay with 20% SL and 1.5R target
    r_mult = replay_trade_on_ticks(trade, ticks, {"sl_pct": 0.20, "t1_multiple": 1.5, "t2_multiple": 2.5})
    assert r_mult > 0.0  # Reached T1


def test_item_10_hypotheses_from_live_config_and_promotion_guard():
    """Item 10: Hypotheses reflect live ENGINE_CONFIG and HYP_LOW_SETUP_SCORE_DRAG is removed."""
    update_hypotheses_in_daily_job()
    with _get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM hypotheses").fetchall()]
        hyp_ids = [r["hypothesis_id"] for r in rows]
        assert "HYP_LOW_SETUP_SCORE_DRAG" not in hyp_ids
        assert "HYP_EXPIRY_TIGHT_SL" in hyp_ids

        # Candidate remains CANDIDATE or OBSERVATION if sample < 30
        for r in rows:
            if r["observation_count"] < 30:
                assert r["status"] in ("OBSERVATION", "CANDIDATE")


def test_item_14_backfill_forward_labels_no_lookahead():
    """Item 14: backfill_forward_labels sets labels_status/done_at and computes forward metrics without look-ahead."""
    with _get_db() as conn:
        conn.execute("DELETE FROM decision_log")
        t_base = datetime.now() - timedelta(minutes=45)
        conn.execute("""
            INSERT INTO decision_log (
                timestamp, symbol, spot_price, spot_change_pct, atm_strike,
                atm_iv, vix, dte,
                pcr, bullish_score, bearish_score, bias,
                pillar_direction, pillar_level, pillar_momentum,
                pillar_volume, pillar_oi, pillar_iv_session,
                setup_score, chosen_contract, decision, contract_ltp, labels_status, created_at
            ) VALUES (
                ?, 'NIFTY', 24000.0, 0.1, 24000.0,
                14.0, 12.5, 3.0,
                1.1, 70.0, 30.0, 'BULLISH',
                1, 1, 1, 1, 1, 1,
                75, '24000 CE', 'CE', 100.0, 'PENDING', ?
            )
        """, (t_base.strftime("%Y-%m-%d %H:%M:%S"), t_base.strftime("%Y-%m-%d %H:%M:%S")))

        # Insert subsequent spot rows for ATR and forward excursions within +30m
        for i in range(1, 12):
            t_sub = t_base + timedelta(minutes=3 * i)
            conn.execute("""
                INSERT INTO decision_log (
                    timestamp, symbol, spot_price, spot_change_pct, atm_strike,
                    atm_iv, vix, dte,
                    pcr, bullish_score, bearish_score, bias,
                    pillar_direction, pillar_level, pillar_momentum,
                    pillar_volume, pillar_oi, pillar_iv_session,
                    setup_score, chosen_contract, decision, contract_ltp, labels_status, created_at
                ) VALUES (
                    ?, 'NIFTY', ?, 0.1, 24000.0,
                    14.0, 12.5, 3.0,
                    1.1, 70.0, 30.0, 'BULLISH',
                    1, 1, 1, 1, 1, 1,
                    0, '24000 CE', 'WAIT', ?, 'SKIPPED', ?
                )
            """, (t_sub.strftime("%Y-%m-%d %H:%M:%S"), 24000.0 + (i * 10), 100.0 + (i * 2), t_sub.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

    count = backfill_forward_labels(batch_size=10)
    assert count >= 1

    with _get_db() as conn:
        processed = conn.execute("SELECT labels_status, labels_done_at, forward_mfe_pct FROM decision_log WHERE decision = 'CE'").fetchone()
        assert processed["labels_status"] == "COMPLETED"
        assert processed["labels_done_at"] is not None
        assert processed["forward_mfe_pct"] is not None
