"""
tests/test_simple_directional_strategy.py — Verification of Simple Directional Strategy with Pullback & Recovery

Explicitly tests all 14 required cases:
1. Strong bullish trend -> BUY CE.
2. Strong bearish trend -> BUY PE.
3. Bullish Price + Momentum + 2/3 confirmations -> BUY CE.
4. Bearish Price + Momentum + 2/3 confirmations -> BUY PE.
5. Bullish Price + Momentum but only 1/3 confirmation -> WAIT.
6. Bearish Price + Momentum but only 1/3 confirmation -> WAIT.
7. Bullish trend -> pullback -> bullish recovery -> BUY CE.
8. Bearish trend -> pullback -> bearish recovery -> BUY PE.
9. Bullish active CE trade -> recovery signal -> WAIT because trade already exists.
10. Bullish active CE trade -> bearish market -> WAIT, do not open PE.
11. CE trade closes -> later valid bullish recovery -> allow new BUY CE.
12. PE trade closes -> later valid bearish recovery -> allow new BUY PE.
13. Non-LIVE data -> preserve existing safety behavior.
14. Invalid option bid/ask -> preserve existing safety behavior.
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

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
    """Helper to build custom test chains."""
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
    payload = {
        "symbol": symbol,
        "spot_price": spot,
        "spot_change_pct": spot_chg,
        "strike_step": step,
        "data_status": data_status,
        "chain": chain,
    }
    if price_history is not None:
        payload["price_history"] = price_history
    return payload


# ==============================================================================
# 14 TEST CASES
# ==============================================================================

def test_1_strong_bullish_trend_generates_buy_ce():
    """1. Strong bullish trend -> BUY CE."""
    data = _make_chain(spot_chg=0.50, pcr=1.30, ce_vol=800000, pe_vol=200000, ce_chg_oi=-30000, pe_chg_oi=100000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["market_direction"] == "BULLISH"
    assert opt["signals"]["price"] == "BULLISH"
    assert opt["signals"]["momentum"] == "BULLISH"


def test_2_strong_bearish_trend_generates_buy_pe():
    """2. Strong bearish trend -> BUY PE."""
    data = _make_chain(spot_chg=-0.50, pcr=0.70, ce_vol=200000, pe_vol=800000, ce_chg_oi=100000, pe_chg_oi=-30000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY PE"
    assert opt["market_direction"] == "BEARISH"
    assert opt["signals"]["price"] == "BEARISH"
    assert opt["signals"]["momentum"] == "BEARISH"


def test_3_bullish_price_momentum_with_2_of_3_confirmations():
    """3. Bullish Price + Momentum + 2/3 confirmations -> BUY CE (OI + Vol bullish, PCR neutral)."""
    # PCR = 0.95 (NEUTRAL), Volume bullish (CE > PE*1.1), OI bullish (PE write > CE write)
    data = _make_chain(spot_chg=0.30, pcr=0.95, ce_vol=500000, pe_vol=100000, ce_chg_oi=-20000, pe_chg_oi=80000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["signals"]["price"] == "BULLISH"
    assert opt["signals"]["momentum"] == "BULLISH"
    assert opt["signals"]["pcr"] == "NEUTRAL"
    assert opt["signals"]["volume"] == "BULLISH"
    assert opt["signals"]["oi"] == "BULLISH"


def test_4_bearish_price_momentum_with_2_of_3_confirmations():
    """4. Bearish Price + Momentum + 2/3 confirmations -> BUY PE (OI + PCR bearish, Volume neutral)."""
    # PCR = 0.80 (BEARISH), Volume neutral (CE == PE), OI bearish (CE write > PE write)
    data = _make_chain(spot_chg=-0.30, pcr=0.80, ce_vol=300000, pe_vol=300000, ce_chg_oi=80000, pe_chg_oi=-20000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY PE"
    assert opt["signals"]["price"] == "BEARISH"
    assert opt["signals"]["momentum"] == "BEARISH"
    assert opt["signals"]["volume"] == "NEUTRAL"
    assert opt["signals"]["pcr"] == "BEARISH"
    assert opt["signals"]["oi"] == "BEARISH"


def test_5_bullish_price_momentum_but_only_1_of_3_confirmation():
    """5. Bullish Price + Momentum but only 1/3 confirmation -> WAIT."""
    # Price + Mom bullish, but PCR neutral (0.95), Vol neutral (300k vs 300k), OI bullish (PE write > CE write) -> only 1/3
    data = _make_chain(spot_chg=0.25, pcr=0.95, ce_vol=300000, pe_vol=300000, ce_chg_oi=-20000, pe_chg_oi=80000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["signals"]["price"] == "BULLISH"
    assert opt["signals"]["momentum"] == "BULLISH"
    assert "confirmation gate failed" in opt["reason"].lower() or "1/3" in opt["reason"]


def test_6_bearish_price_momentum_but_only_1_of_3_confirmation():
    """6. Bearish Price + Momentum but only 1/3 confirmation -> WAIT."""
    # Price + Mom bearish, but PCR neutral (0.95), Vol neutral (300k vs 300k), OI bearish -> only 1/3
    data = _make_chain(spot_chg=-0.25, pcr=0.95, ce_vol=300000, pe_vol=300000, ce_chg_oi=80000, pe_chg_oi=-20000)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["signals"]["price"] == "BEARISH"
    assert opt["signals"]["momentum"] == "BEARISH"
    assert "confirmation gate failed" in opt["reason"].lower() or "1/3" in opt["reason"]


def test_7_bullish_trend_pullback_recovery_generates_buy_ce():
    """7. Bullish trend -> pullback -> bullish recovery -> BUY CE with PULLBACK_RECOVERY setup."""
    # Spot pulled back from 22550 to 22510, now recovering to 22530
    price_hist = [22480.0, 22550.0, 22510.0, 22530.0]
    data = _make_chain(spot=22530.0, spot_chg=0.35, pcr=1.20, ce_vol=600000, pe_vol=100000, price_history=price_hist)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["setup_type"] == "PULLBACK_RECOVERY"


def test_8_bearish_trend_pullback_recovery_generates_buy_pe():
    """8. Bearish trend -> pullback (bounce) -> bearish recovery -> BUY PE with PULLBACK_RECOVERY setup."""
    # Spot bounced from 22400 to 22460, now turning back down to 22430
    price_hist = [22500.0, 22400.0, 22460.0, 22430.0]
    data = _make_chain(spot=22430.0, spot_chg=-0.35, pcr=0.75, ce_vol=100000, pe_vol=600000, ce_chg_oi=80000, pe_chg_oi=-20000, price_history=price_hist)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY PE"
    assert opt["setup_type"] == "PULLBACK_RECOVERY"


def test_9_bullish_active_ce_trade_prevents_duplicate_entry():
    """9. Bullish active CE trade -> recovery signal -> WAIT because trade already exists."""
    price_hist = [22480.0, 22550.0, 22510.0, 22530.0]
    data = _make_chain(spot=22530.0, spot_chg=0.35, pcr=1.20, ce_vol=600000, pe_vol=100000, price_history=price_hist)
    res = analyze_option_desk(data, has_active_trade=True)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["reason"] == "ACTIVE TRADE EXISTS"
    assert opt["active_trade_exists"] is True


def test_10_bullish_active_ce_trade_in_bearish_market_does_not_open_pe():
    """10. Bullish active CE trade -> bearish market -> WAIT, do not open PE."""
    data = _make_chain(spot_chg=-0.50, pcr=0.70, ce_vol=100000, pe_vol=800000, ce_chg_oi=100000, pe_chg_oi=-30000)
    res = analyze_option_desk(data, has_active_trade=True)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["reason"] == "ACTIVE TRADE EXISTS"
    assert opt["active_trade_exists"] is True


def test_11_ce_trade_closed_allows_new_buy_ce():
    """11. CE trade closes -> later valid bullish recovery -> allow new BUY CE."""
    # When active trade is False (closed), fresh valid recovery enters BUY CE
    price_hist = [22480.0, 22550.0, 22510.0, 22535.0]
    data = _make_chain(spot=22535.0, spot_chg=0.35, pcr=1.20, ce_vol=600000, pe_vol=100000, price_history=price_hist)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["setup_type"] == "PULLBACK_RECOVERY"


def test_12_pe_trade_closed_allows_new_buy_pe():
    """12. PE trade closes -> later valid bearish recovery -> allow new BUY PE."""
    price_hist = [22500.0, 22400.0, 22460.0, 22425.0]
    data = _make_chain(spot=22425.0, spot_chg=-0.35, pcr=0.75, ce_vol=100000, pe_vol=600000, ce_chg_oi=80000, pe_chg_oi=-20000, price_history=price_hist)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY PE"
    assert opt["setup_type"] == "PULLBACK_RECOVERY"


def test_13_non_live_data_fails_closed_to_wait():
    """13. Non-LIVE data -> preserve existing safety behavior."""
    data = _make_chain(spot_chg=0.50, pcr=1.30, ce_vol=800000, pe_vol=200000, data_status="DATA_UNAVAILABLE")
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["data_status"] == "DATA_UNAVAILABLE"


def test_14_invalid_option_bid_ask_fails_closed_to_wait():
    """14. Invalid option bid/ask -> preserve existing safety behavior."""
    data = _make_chain(spot_chg=0.50, pcr=1.30, ce_vol=800000, pe_vol=200000, valid_quotes=False)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "Option quality gate failed" in opt["reason"]
