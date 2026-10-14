"""
tests/test_execution_realism_and_costs.py
Test suite validating P0 & P1 execution realism, quote freshness gating, and statutory transaction cost accounting:
1. Target 1 requires Bid >= T1 (not just LTP >= T1) when fresh quotes exist.
2. Stale / fallback quotes are display-only and cannot trigger regular order fills or exits.
3. Realized net PnL (net_pnl_inr) deducts statutory transaction costs via calculate_trade_costs.
4. Harmonized CONSECUTIVE_CONFIRMATIONS_REQUIRED configuration.
"""

import os
import pytest
from unittest.mock import patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.options_signal_service import (
    _get_db,
    init_signal_db,
    record_signal,
    update_active_signals,
    close_signal_manually,
    get_signals_summary,
    SIGNAL_EXEC_CONFIG,
    MANUALLY_CLOSED,
)
from app.services.options_engine import (
    ENGINE_CONFIG,
    update_symbol_bias_state,
    get_symbol_bias_state,
    _BIAS_STATES,
)
from app.services.options_replay_backtest import calculate_trade_costs

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def setup_db():
    init_signal_db()
    with _get_db() as conn:
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM trade_ticks")
    yield


def test_target1_requires_bid_above_t1_not_just_ltp():
    """LTP exceeding T1 while Bid is below T1 must NOT trigger Target 1 fill."""
    assert SIGNAL_EXEC_CONFIG["USE_BID_FOR_MFE_TRAILING"] is True
    assert SIGNAL_EXEC_CONFIG["CREDIT_T1_T2_AT_OBSERVED_BID"] is True

    future_exp = (datetime.now(IST) + timedelta(days=7)).strftime("%d-%b-%Y")
    sig = record_signal({
        "symbol": "NIFTY",
        "type": "CE",
        "strike": 22500,
        "expiry": future_exp,
        "spot_price": 22450.0,
        "entry_price": 100.0,
        "bid_at_entry": 99.5,
        "ask_at_entry": 100.5,
        "stop_loss": 85.0,
        "target_1": 130.0,
        "target_2": 160.0,
        "lots": 1,
        "lot_size": 65,
    }, is_paper_trade=True, override_cooldown=True)
    assert sig is not None
    sig_id = sig["id"]

    midday = datetime.now(IST).replace(hour=11, minute=30, second=0)

    # 1. Wide spread: LTP is 132.0 (>= T1 130), but Bid is only 128.0 (< T1 130)
    market_cache_wide_spread = {
        "NIFTY": {
            "spot_price": 22500.0,
            "chain": [
                {"strike": 22500, "ce_ltp": 132.0, "ce_bid": 128.0, "ce_ask": 132.5}
            ]
        }
    }
    updated = update_active_signals(market_data_cache=market_cache_wide_spread, now_dt=midday)
    assert len(updated) == 1
    # Trade must remain ACTIVE because observed Bid (128.0) did NOT hit T1 (130.0)
    assert updated[0]["status"] == "ACTIVE"

    # 2. Bid catches up to 131.0 (>= T1 130)
    market_cache_t1_hit = {
        "NIFTY": {
            "spot_price": 22510.0,
            "chain": [
                {"strike": 22500, "ce_ltp": 132.0, "ce_bid": 131.0, "ce_ask": 132.5}
            ]
        }
    }
    updated2 = update_active_signals(market_data_cache=market_cache_t1_hit, now_dt=midday + timedelta(minutes=1))
    assert len(updated2) == 1
    # Now T1 must be filled
    assert updated2[0]["status"] == "TARGET_1_HIT"


def test_stale_fallback_quotes_do_not_trigger_fills_or_exits():
    """Missing or non-positive quotes fall back for display only and do not trigger regular exits."""
    future_exp = (datetime.now(IST) + timedelta(days=7)).strftime("%d-%b-%Y")
    midday = datetime.now(IST).replace(hour=11, minute=30, second=0)
    sig = record_signal({
        "symbol": "NIFTY",
        "type": "CE",
        "strike": 22500,
        "expiry": future_exp,
        "spot_price": 22450.0,
        "entry_price": 100.0,
        "bid_at_entry": 99.5,
        "ask_at_entry": 100.5,
        "stop_loss": 85.0,
        "target_1": 130.0,
        "target_2": 160.0,
        "lots": 1,
        "lot_size": 65,
    }, is_paper_trade=True, override_cooldown=True, now_dt=midday)
    assert sig is not None
    sig_id = sig["id"]


    # Empty cache / missing strike in chain (has_valid_new_quote == False)
    empty_cache = {
        "NIFTY": {
            "spot_price": 22450.0,
            "chain": []
        }
    }

    # Signal was created at entry=100.5. If fallback bid was somehow 80 (below SL),
    # it must NOT trigger SL_HIT when quotes are missing.
    with _get_db() as conn:
        conn.execute("UPDATE signals SET current_price = 80.0, bid_at_exit = 80.0 WHERE id = ?", (sig_id,))

    with patch("app.services.options_signal_service.fetch_option_chain_data", return_value={"chain": []}):
        updated = update_active_signals(market_data_cache=empty_cache, now_dt=midday)

    assert len(updated) == 1
    # Must NOT have exited with SL_HIT because has_valid_new_quote is False
    assert updated[0]["status"] == "ACTIVE"
    assert updated[0]["current_price"] == 80.0


def test_net_pnl_inr_deducts_statutory_costs_on_exit():
    """Verify that net_pnl_inr on closed trades is accurately net of statutory F&O transaction costs."""
    future_exp = (datetime.now(IST) + timedelta(days=7)).strftime("%d-%b-%Y")
    sig = record_signal({
        "symbol": "NIFTY",
        "type": "CE",
        "strike": 22500,
        "expiry": future_exp,
        "spot_price": 22450.0,
        "entry_price": 100.0,
        "bid_at_entry": 99.5,
        "ask_at_entry": 100.5,
        "stop_loss": 85.0,
        "target_1": 130.0,
        "target_2": 160.0,
        "lots": 1,
        "lot_size": 65,
    }, is_paper_trade=True, override_cooldown=True)
    assert sig is not None
    actual_entry = sig["entry_price"]  # realistically bought at Ask = 100.5

    midday = datetime.now(IST).replace(hour=11, minute=30, second=0)

    # Target 2 hit at Bid = 162.0
    market_cache_t2 = {
        "NIFTY": {
            "spot_price": 22550.0,
            "chain": [
                {"strike": 22500, "ce_ltp": 162.5, "ce_bid": 162.0, "ce_ask": 163.0}
            ]
        }
    }
    updated = update_active_signals(market_data_cache=market_cache_t2, now_dt=midday)
    assert len(updated) == 1
    closed = updated[0]
    assert closed["status"] == "TARGET_2_HIT"

    gross_pts = 162.0 - actual_entry
    gross_inr = round(gross_pts * 65 * 1, 2)

    # Calculate exact expected statutory costs
    costs = calculate_trade_costs(
        entry_price=actual_entry,
        exit_prices=[(162.0, 1.0)],
        lot_size=65,
        lots=1,
        symbol="NIFTY",
    )
    expected_costs_inr = costs["total_costs_inr"]
    assert expected_costs_inr > 0.0

    expected_net_inr = round(gross_inr - expected_costs_inr, 2)
    assert closed["net_pnl_inr"] == pytest.approx(expected_net_inr, abs=0.05)
    assert closed["net_pnl_inr"] < gross_inr


def test_manual_close_deducts_statutory_costs():
    """Manual close accurately calculates and deducts statutory transaction costs."""
    future_exp = (datetime.now(IST) + timedelta(days=7)).strftime("%d-%b-%Y")
    sig = record_signal({
        "symbol": "NIFTY",
        "type": "PE",
        "strike": 22400,
        "expiry": future_exp,
        "spot_price": 22450.0,
        "entry_price": 120.0,
        "bid_at_entry": 119.5,
        "ask_at_entry": 120.5,
        "stop_loss": 100.0,
        "target_1": 150.0,
        "target_2": 180.0,
        "lots": 1,
        "lot_size": 65,
    }, is_paper_trade=True, lots=1, override_cooldown=True)
    assert sig is not None
    sig_id = sig["id"]
    actual_entry = sig["entry_price"]  # 120.5

    closed = close_signal_manually(sig_id, exit_price=140.0, exit_bid=139.5, exit_reason=MANUALLY_CLOSED)
    assert closed is not None
    assert closed["status"] == "CLOSED"

    gross_pts = 139.5 - actual_entry  # +19.0 pts
    gross_inr = round(gross_pts * 65 * 1, 2)  # ₹1,235.00

    costs = calculate_trade_costs(
        entry_price=actual_entry,
        exit_prices=[(139.5, 1.0)],
        lot_size=65,
        lots=1,
        symbol="NIFTY",
    )
    expected_net_inr = round(gross_inr - costs["total_costs_inr"], 2)
    assert closed["net_pnl_inr"] == pytest.approx(expected_net_inr, abs=0.05)
    assert closed["net_pnl_inr"] < gross_inr


def test_engine_config_consecutive_confirmations_harmonized():
    """Harmonized CONSECUTIVE_CONFIRMATIONS_REQUIRED matches between config and bias tracking state update."""
    config_val = ENGINE_CONFIG.get("CONSECUTIVE_CONFIRMATIONS_REQUIRED", 1)
    assert config_val >= 1

    # Initialize bias state for test symbol
    _BIAS_STATES["TEST_INDEX"] = {
        "last_bias": "NEUTRAL",
        "pending_bias": "NEUTRAL",
        "last_changed_time": "11:00",
        "consecutive_confirmations": 0,
    }

    # When config is 1 confirmation, raw_bias transitions on first check
    new_bias = update_symbol_bias_state("TEST_INDEX", "BULLISH", score_diff=15.0)
    assert new_bias == "BULLISH"
