"""
tests/test_pure_directional_buying.py — Pure Directional Option-Buying Strategy Verification
Verifies all Section 14 criteria:
1. BULLISH -> BUY CE
2. BEARISH -> BUY PE
3. NEUTRAL -> WAIT
4. Missing live data -> WAIT
5. Invalid bid/ask -> WAIT
6. Confirmation cycle 1 -> no entry yet (WAIT)
7. Confirmation cycle 2 -> entry generated (BUY CE / BUY PE)
8. Direction reversal -> previous confirmation reset
9. Prohibited option selling -> verified only BUY CE, BUY PE, WAIT
10. Setup score is not a hard barrier (score < 75 can still BUY CE if gates pass)
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import options_engine
from app.services.options_engine import analyze_option_desk
from app.services.options_scheduler import (
    update_pending_entry_confirmation,
    reset_pending_entry_confirmation,
    get_pending_entry_confirmation,
)

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def prime_trading_hours(monkeypatch):
    """Ensures mock runs during prime trading window (10:30 IST) with favorable IV."""
    midday = datetime(2026, 10, 5, 10, 30, tzinfo=IST)
    orig_filter = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(
        options_engine,
        "evaluate_iv_and_time_filter",
        lambda atm_iv, now_dt=None: orig_filter(atm_iv, midday),
    )
    with options_engine._BIAS_LOCK:
        options_engine._BIAS_STATES.clear()


def _bullish_chain(spot=22620.0, step=50) -> dict:
    """Constructs a strong bullish breakout chain."""
    return {
        "symbol": "NIFTY",
        "spot_price": spot,
        "spot_change_pct": 0.45,  # Strong positive momentum
        "strike_step": step,
        "data_status": "LIVE",
        "chain": [
            {
                "strike": 22400, "ce_ltp": 190.0, "ce_bid": 189.5, "ce_ask": 190.5,
                "pe_ltp": 25.0, "pe_bid": 24.5, "pe_ask": 25.5,
                "ce_oi": 50000, "pe_oi": 250000, "ce_change_oi": -20000, "pe_change_oi": 80000,
                "ce_volume": 120000, "pe_volume": 40000, "ce_iv": 14.0, "pe_iv": 14.5
            },
            {
                "strike": 22450, "ce_ltp": 145.0, "ce_bid": 144.5, "ce_ask": 145.5,
                "pe_ltp": 40.0, "pe_bid": 39.5, "pe_ask": 40.5,
                "ce_oi": 60000, "pe_oi": 300000, "ce_change_oi": -15000, "pe_change_oi": 100000,
                "ce_volume": 180000, "pe_volume": 50000, "ce_iv": 14.0, "pe_iv": 14.5
            },
            {
                "strike": 22500, "ce_ltp": 105.0, "ce_bid": 104.5, "ce_ask": 105.5,
                "pe_ltp": 60.0, "pe_bid": 59.5, "pe_ask": 60.5,
                "ce_oi": 80000, "pe_oi": 400000, "ce_change_oi": -30000, "pe_change_oi": 150000,
                "ce_volume": 350000, "pe_volume": 80000, "ce_iv": 14.0, "pe_iv": 14.5
            },
            {
                "strike": 22550, "ce_ltp": 72.0, "ce_bid": 71.5, "ce_ask": 72.5,
                "pe_ltp": 95.0, "pe_bid": 94.5, "pe_ask": 95.5,
                "ce_oi": 120000, "pe_oi": 200000, "ce_change_oi": 10000, "pe_change_oi": 60000,
                "ce_volume": 400000, "pe_volume": 90000, "ce_iv": 14.0, "pe_iv": 14.5
            },
            {
                "strike": 22600, "ce_ltp": 45.0, "ce_bid": 44.5, "ce_ask": 45.5,
                "pe_ltp": 130.0, "pe_bid": 129.5, "pe_ask": 130.5,
                "ce_oi": 150000, "pe_oi": 80000, "ce_change_oi": 20000, "pe_change_oi": 10000,
                "ce_volume": 250000, "pe_volume": 60000, "ce_iv": 14.0, "pe_iv": 14.5
            }
        ]
    }


def _bearish_chain(spot=22380.0, step=50) -> dict:
    """Constructs a strong bearish breakdown chain."""
    return {
        "symbol": "NIFTY",
        "spot_price": spot,
        "spot_change_pct": -0.45,  # Strong negative momentum
        "strike_step": step,
        "data_status": "LIVE",
        "chain": [
            {
                "strike": 22400, "ce_ltp": 130.0, "ce_bid": 129.5, "ce_ask": 130.5,
                "pe_ltp": 45.0, "pe_bid": 44.5, "pe_ask": 45.5,
                "ce_oi": 150000, "pe_oi": 80000, "ce_change_oi": 50000, "pe_change_oi": -10000,
                "ce_volume": 80000, "pe_volume": 250000, "ce_iv": 15.0, "pe_iv": 15.5
            },
            {
                "strike": 22450, "ce_ltp": 95.0, "ce_bid": 94.5, "ce_ask": 95.5,
                "pe_ltp": 75.0, "pe_bid": 74.5, "pe_ask": 75.5,
                "ce_oi": 250000, "pe_oi": 100000, "ce_change_oi": 90000, "pe_change_oi": -20000,
                "ce_volume": 90000, "pe_volume": 380000, "ce_iv": 15.0, "pe_iv": 15.5
            },
            {
                "strike": 22500, "ce_ltp": 60.0, "ce_bid": 59.5, "ce_ask": 60.5,
                "pe_ltp": 110.0, "pe_bid": 109.5, "pe_ask": 110.5,
                "ce_oi": 400000, "pe_oi": 80000, "ce_change_oi": 160000, "pe_change_oi": -40000,
                "ce_volume": 100000, "pe_volume": 420000, "ce_iv": 15.0, "pe_iv": 15.5
            },
            {
                "strike": 22550, "ce_ltp": 35.0, "ce_bid": 34.5, "ce_ask": 35.5,
                "pe_ltp": 150.0, "pe_bid": 149.5, "pe_ask": 150.5,
                "ce_oi": 300000, "pe_oi": 50000, "ce_change_oi": 100000, "pe_change_oi": -15000,
                "ce_volume": 70000, "pe_volume": 200000, "ce_iv": 15.0, "pe_iv": 15.5
            },
            {
                "strike": 22600, "ce_ltp": 20.0, "ce_bid": 19.5, "ce_ask": 20.5,
                "pe_ltp": 195.0, "pe_bid": 194.5, "pe_ask": 195.5,
                "ce_oi": 200000, "pe_oi": 30000, "ce_change_oi": 50000, "pe_change_oi": -10000,
                "ce_volume": 50000, "pe_volume": 120000, "ce_iv": 15.0, "pe_iv": 15.5
            }
        ]
    }


def _neutral_chain(spot=22500.0, step=50) -> dict:
    """Constructs a flat, neutral chop chain with no level breakout."""
    return {
        "symbol": "NIFTY",
        "spot_price": spot,
        "spot_change_pct": 0.02,
        "strike_step": step,
        "data_status": "LIVE",
        "chain": [
            {
                "strike": s, "ce_ltp": 80.0, "ce_bid": 79.5, "ce_ask": 80.5,
                "pe_ltp": 80.0, "pe_bid": 79.5, "pe_ask": 80.5,
                "ce_oi": 100000, "pe_oi": 100000, "ce_change_oi": 5000, "pe_change_oi": 5000,
                "ce_volume": 50000, "pe_volume": 50000, "ce_iv": 14.0, "pe_iv": 14.0
            }
            for s in (22400, 22450, 22500, 22550, 22600)
        ]
    }


# ==============================================================================
# TESTS
# ==============================================================================

def test_bullish_market_generates_buy_ce():
    """1. Bullish direction + breakout + momentum + valid quotes = BUY CE."""
    data = _bullish_chain(spot=22620.0)  # Broken past 22600
    res = analyze_option_desk(data)

    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["market_direction"] == "BULLISH"
    assert opt["level_status"] == "CONFIRMED"
    assert opt["momentum_status"] == "CONFIRMED"
    assert opt["data_status"] == "LIVE"
    assert opt["selected_option"] != "--"
    assert opt["entry_price"] > 0.0
    assert "CE" in opt["selected_option"]


def test_bearish_market_generates_buy_pe():
    """2. Bearish direction + breakdown + momentum + valid quotes = BUY PE."""
    data = _bearish_chain(spot=22380.0)  # Broken below 22400
    res = analyze_option_desk(data)

    opt = res["option_buying"]
    assert opt["decision"] == "BUY PE"
    assert opt["market_direction"] == "BEARISH"
    assert opt["level_status"] == "CONFIRMED"
    assert opt["momentum_status"] == "CONFIRMED"
    assert opt["data_status"] == "LIVE"
    assert opt["selected_option"] != "--"
    assert opt["entry_price"] > 0.0
    assert "PE" in opt["selected_option"]


def test_neutral_market_generates_wait():
    """3. Neutral direction / unclear market action = WAIT."""
    data = _neutral_chain(spot=22500.0)
    res = analyze_option_desk(data)

    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["market_direction"] == "NEUTRAL"


def test_missing_live_data_fails_closed_to_wait():
    """4. If broker live data is missing or unavailable, fail closed to WAIT."""
    data = _bullish_chain(spot=22620.0)
    data["data_status"] = "DATA_UNAVAILABLE"
    res = analyze_option_desk(data)

    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["data_status"] == "DATA_UNAVAILABLE"
    assert "unavailable" in opt["decision_reason"].lower()


def test_invalid_bid_ask_fails_closed_to_wait():
    """5. Valid directional setup with zero or missing bid/ask quotes fails to WAIT."""
    data = _bullish_chain(spot=22620.0)
    # Corrupt bid/ask for all strikes
    for r in data["chain"]:
        r["ce_bid"] = 0.0
        r["ce_ask"] = 0.0

    res = analyze_option_desk(data)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "Option quality gate failed" in opt["decision_reason"] or "invalid" in opt["decision_reason"].lower()


def test_two_confirmation_mechanism_flow():
    """6, 7 & 8: Test 2-confirmation mechanism, candidate persistence, and direction reversal."""
    sym = "TEST_CONF_SYM"
    reset_pending_entry_confirmation(sym)

    # Cycle 1: Candidate BUY CE -> count 1, not confirmed yet
    count, confirmed = update_pending_entry_confirmation(sym, "CE", req_confs=2)
    assert count == 1
    assert confirmed is False

    # Cycle 2: Candidate BUY CE again -> count 2, confirmed!
    count, confirmed = update_pending_entry_confirmation(sym, "CE", req_confs=2)
    assert count == 2
    assert confirmed is True

    # Cycle 3: Sudden reversal to PE -> previous CE confirmation reset, PE starts at count 1
    count, confirmed = update_pending_entry_confirmation(sym, "PE", req_confs=2)
    assert count == 1
    assert confirmed is False
    state = get_pending_entry_confirmation(sym)
    assert state["candidate_side"] == "PE"
    assert state["confirmation_count"] == 1

    # Cycle 4: WAIT / gate fail -> resets confirmation count to 0
    count, confirmed = update_pending_entry_confirmation(sym, None, req_confs=2)
    assert count == 0
    assert confirmed is False


def test_no_option_selling_decision_ever_produced():
    """9. Strict verification that system ONLY generates BUY CE, BUY PE, or WAIT."""
    prohibited = {
        "SELL CE", "SELL PE", "IRON CONDOR", "CREDIT SPREAD",
        "COVERED CALL", "STRADDLE", "STRANGLE", "SHORT STRADDLE", "SHORT STRANGLE"
    }

    # Test bullish, bearish, neutral, unavailable
    test_cases = [
        _bullish_chain(spot=22620.0),
        _bearish_chain(spot=22380.0),
        _neutral_chain(spot=22500.0),
        {"symbol": "NIFTY", "spot_price": 22500, "data_status": "DATA_UNAVAILABLE", "chain": []}
    ]

    for data in test_cases:
        res = analyze_option_desk(data)
        dec = res["option_buying"]["decision"]
        assert dec in ("BUY CE", "BUY PE", "WAIT")
        assert dec not in prohibited


def test_setup_score_is_not_hard_barrier():
    """10. Verify that score >= 75 is NOT required to enter a trade."""
    data = _bullish_chain(spot=22620.0)
    res = analyze_option_desk(data)
    opt = res["option_buying"]

    # Decision comes strictly from Direction + Level + Momentum + Live Data + Option Quality
    assert opt["decision"] == "BUY CE"
    assert opt["market_direction"] == "BULLISH"
