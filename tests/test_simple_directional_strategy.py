"""
tests/test_simple_directional_strategy.py — Verification of Simple Directional Strategy with Anti-Chase & Pullback Recovery

Explicitly tests all required Section 16 test cases:
1. Fresh bullish high -> WAIT with FRESH_HIGH_NO_PULLBACK.
2. Bullish pullback and recovery -> BUY CE with PULLBACK_RECOVERY.
3. Weak bounce -> WAIT.
4. Active CE trade -> WAIT with ACTIVE_TRADE_EXISTS.
5. Opposite direction while CE active -> WAIT with ACTIVE_TRADE_EXISTS.
6. Re-entry after CE closes -> BUY CE.
7. Bearish equivalent -> BUY PE.
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import options_engine
from app.services.options_engine import analyze_option_desk, clear_symbol_price_history

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    """Ensures mock runs during prime trading window (10:30 IST) with favorable IV and clean state."""
    midday = datetime(2026, 10, 5, 10, 30, tzinfo=IST)
    orig_filter = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(
        options_engine,
        "evaluate_iv_and_time_filter",
        lambda atm_iv, now_dt=None: orig_filter(atm_iv, midday),
    )
    with options_engine._BIAS_LOCK:
        options_engine._BIAS_STATES.clear()
    clear_symbol_price_history()


def _make_chain(
    symbol="NIFTY",
    spot=22500.0,
    spot_chg=0.45,
    pcr=1.25,
    ce_vol=500000,
    pe_vol=100000,
    ce_chg_oi=-20000,
    pe_chg_oi=80000,
    data_status="LIVE",
    valid_quotes=True,
    price_history=None
) -> dict:
    """Helper to build custom test chains with default valid recovery or custom price history."""
    step = 50
    strikes = [22400, 22450, 22500, 22550, 22600]
    chain = []
    for s in strikes:
        chain.append({
            "strike": s,
            "ce_ltp": 100.0,
            "ce_bid": 99.5 if valid_quotes else 0.0,
            "ce_ask": 100.5 if valid_quotes else 0.0,
            "pe_ltp": 50.0,
            "pe_bid": 49.5 if valid_quotes else 0.0,
            "pe_ask": 50.5 if valid_quotes else 0.0,
            "ce_oi": 100000,
            "pe_oi": int(100000 * pcr),
            "ce_change_oi": ce_chg_oi,
            "pe_change_oi": pe_chg_oi,
            "ce_volume": ce_vol,
            "pe_volume": pe_vol,
            "ce_iv": 18.0,
            "pe_iv": 18.5,
        })

    if price_history is None:
        if spot_chg > 0:
            price_history = [spot - 30.0, spot + 20.0, spot - 20.0, spot]
        elif spot_chg < 0:
            price_history = [spot + 30.0, spot - 20.0, spot + 20.0, spot]
        else:
            price_history = [spot, spot, spot]

    payload = {
        "symbol": symbol,
        "spot_price": spot,
        "spot_change_pct": spot_chg,
        "strike_step": step,
        "data_status": data_status,
        "chain": chain,
        "price_history": price_history,
    }
    return payload


# ==============================================================================
# SECTION 16 EXACT TEST CASES
# ==============================================================================

def test_section16_case1_fresh_bullish_high_blocks_chase():
    """Test 1: Price bullish, Mom bullish, OI/Vol/PCR bullish, New intraday high, No pullback -> WAIT, FRESH_HIGH_NO_PULLBACK."""
    # 100 -> 105 -> 110 -> 115 (monotonic fresh high, no pullback)
    fresh_high_history = [100.0, 105.0, 110.0, 115.0]
    data = _make_chain(
        spot=115.0,
        spot_chg=0.50,
        pcr=1.30,
        ce_vol=800000,
        pe_vol=200000,
        ce_chg_oi=-30000,
        pe_chg_oi=100000,
        price_history=fresh_high_history
    )
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["reason"] == "FRESH_HIGH_NO_PULLBACK"
    assert opt["setup_type"] == "FRESH_HIGH_NO_PULLBACK"


def test_section16_case2_bullish_pullback_and_recovery_generates_buy_ce():
    """Test 2: Bullish trend, New high, Pullback, Recovery, Price+Mom bullish, 2/3 confirmations, No active trade -> BUY CE."""
    # 100 -> 105 -> 110 -> 115 -> 112 -> 109 -> 111 -> 113 (pullback to 109, recovering to 113)
    recovery_history = [100.0, 105.0, 110.0, 115.0, 112.0, 109.0, 111.0, 113.0]
    data = _make_chain(
        spot=113.0,
        spot_chg=0.40,
        pcr=1.25,
        ce_vol=600000,
        pe_vol=100000,
        ce_chg_oi=-20000,
        pe_chg_oi=80000,
        price_history=recovery_history
    )
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["setup_type"] == "PULLBACK_RECOVERY"


def test_section16_case3_weak_bounce_momentum_not_bullish():
    """Test 3: Bullish trend, Pullback, Tiny bounce, Momentum not bullish -> WAIT."""
    # 100 -> 115 -> 109 -> 109.1 (tiny flat bounce, spot_chg flat/weak)
    weak_bounce_history = [100.0, 115.0, 109.0, 109.1]
    data = _make_chain(
        spot=109.1,
        spot_chg=0.01,  # Below MOMENTUM_CHANGE_THRESHOLD (0.05)
        pcr=1.25,
        ce_vol=600000,
        pe_vol=100000,
        price_history=weak_bounce_history
    )
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"


def test_section16_case4_active_ce_trade_waits_with_active_trade_exists():
    """Test 4: Bullish trend, Pullback, Recovery, All conditions valid, CE trade already active -> WAIT, ACTIVE_TRADE_EXISTS."""
    recovery_history = [100.0, 105.0, 110.0, 115.0, 112.0, 109.0, 111.0, 113.0]
    data = _make_chain(
        spot=113.0,
        spot_chg=0.40,
        pcr=1.25,
        ce_vol=600000,
        pe_vol=100000,
        price_history=recovery_history
    )
    res = analyze_option_desk(data, has_active_trade=True)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["reason"] == "ACTIVE TRADE EXISTS" or opt["reason"] == "ACTIVE_TRADE_EXISTS"
    assert opt["active_trade_exists"] is True


def test_section16_case5_opposite_direction_while_ce_active():
    """Test 5: CE active, Market turns bearish, PE conditions valid -> WAIT, ACTIVE_TRADE_EXISTS."""
    data = _make_chain(
        spot_chg=-0.50,
        pcr=0.70,
        ce_vol=100000,
        pe_vol=800000,
        ce_chg_oi=100000,
        pe_chg_oi=-30000
    )
    res = analyze_option_desk(data, has_active_trade=True)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["reason"] == "ACTIVE TRADE EXISTS" or opt["reason"] == "ACTIVE_TRADE_EXISTS"
    assert opt["active_trade_exists"] is True


def test_section16_case6_reentry_after_ce_closes():
    """Test 6: Previous CE closed, No active trade, New bullish pullback, Recovery, 2/3 confirmation -> BUY CE."""
    recovery_history = [22480.0, 22550.0, 22510.0, 22535.0]
    data = _make_chain(
        spot=22535.0,
        spot_chg=0.35,
        pcr=1.20,
        ce_vol=600000,
        pe_vol=100000,
        price_history=recovery_history
    )
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["setup_type"] == "PULLBACK_RECOVERY"


def test_section16_case7_bearish_pullback_and_recovery_generates_buy_pe():
    """Test 7: Bearish trend, Downward rebound/pullback, Bearish recovery, 2/3 bearish confirmation, No active trade -> BUY PE."""
    # 22500 -> 22400 -> 22460 -> 22430 (rebounded to 22460, now turning back down to 22430)
    bearish_recovery_hist = [22500.0, 22400.0, 22460.0, 22430.0]
    data = _make_chain(
        spot=22430.0,
        spot_chg=-0.35,
        pcr=0.75,
        ce_vol=100000,
        pe_vol=600000,
        ce_chg_oi=80000,
        pe_chg_oi=-20000,
        price_history=bearish_recovery_hist
    )
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY PE"
    assert opt["setup_type"] == "PULLBACK_RECOVERY"


# ==============================================================================
# ADDITIONAL INTEGRITY & SAFETY CASES
# ==============================================================================

def test_8_bullish_price_momentum_with_2_of_3_confirmations():
    """8. Bullish Price + Momentum + 2/3 confirmations -> BUY CE (OI + Vol bullish, PCR neutral)."""
    data = _make_chain(spot_chg=0.30, pcr=0.95, ce_vol=500000, pe_vol=100000, ce_chg_oi=-20000, pe_chg_oi=80000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["signals"]["price"] == "BULLISH"
    assert opt["signals"]["momentum"] == "BULLISH"
    assert opt["signals"]["pcr"] == "NEUTRAL"
    assert opt["signals"]["volume"] == "BULLISH"
    assert opt["signals"]["oi"] == "BULLISH"


def test_9_bearish_price_momentum_with_2_of_3_confirmations():
    """9. Bearish Price + Momentum + 2/3 confirmations -> BUY PE (OI + PCR bearish, Volume neutral)."""
    data = _make_chain(spot_chg=-0.30, pcr=0.80, ce_vol=300000, pe_vol=300000, ce_chg_oi=80000, pe_chg_oi=-20000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY PE"
    assert opt["signals"]["price"] == "BEARISH"
    assert opt["signals"]["momentum"] == "BEARISH"
    assert opt["signals"]["volume"] == "NEUTRAL"
    assert opt["signals"]["pcr"] == "BEARISH"
    assert opt["signals"]["oi"] == "BEARISH"


def test_10_bullish_price_momentum_but_only_1_of_3_confirmation():
    """10. Bullish Price + Momentum but only 1/3 confirmation -> WAIT."""
    data = _make_chain(spot_chg=0.25, pcr=0.95, ce_vol=300000, pe_vol=300000, ce_chg_oi=-20000, pe_chg_oi=80000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "confirmation gate failed" in opt["reason"].lower() or "1/3" in opt["reason"]


def test_11_bearish_price_momentum_but_only_1_of_3_confirmation():
    """11. Bearish Price + Momentum but only 1/3 confirmation -> WAIT."""
    data = _make_chain(spot_chg=-0.25, pcr=0.95, ce_vol=300000, pe_vol=300000, ce_chg_oi=80000, pe_chg_oi=-20000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "confirmation gate failed" in opt["reason"].lower() or "1/3" in opt["reason"]


def test_12_non_live_data_fails_closed_to_wait():
    """12. Non-LIVE data -> preserve existing safety behavior."""
    data = _make_chain(spot_chg=0.50, pcr=1.30, ce_vol=800000, pe_vol=200000, data_status="DATA_UNAVAILABLE")
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["data_status"] == "DATA_UNAVAILABLE"


def test_13_invalid_option_bid_ask_fails_closed_to_wait():
    """13. Invalid option bid/ask -> preserve existing safety behavior."""
    data = _make_chain(spot_chg=0.50, pcr=1.30, ce_vol=800000, pe_vol=200000, valid_quotes=False)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "Option quality gate failed" in opt["reason"]
