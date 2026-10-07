"""
tests/test_market_expansion_and_chop.py — Verification of Market Expansion, Chop Prevention, and Directional Buying Logic

Validates:
1. Strong directional move at 12:15 can still generate a trade (no midday ban).
2. Sideways/choppy market at 10:30 generates WAIT with reason LOW_EXPANSION_OR_CHOP.
3. Pullback + recovery + strong expansion generates BUY CE / BUY PE.
4. Fresh high/low without pullback generates WAIT with FRESH_HIGH_NO_PULLBACK / FRESH_LOW_NO_PULLBACK.
5. Existing active trade prevents another entry.
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import options_engine
from app.services.options_engine import analyze_option_desk, clear_symbol_price_history

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def clean_state():
    with options_engine._BIAS_LOCK:
        options_engine._BIAS_STATES.clear()
    clear_symbol_price_history()


def _make_chain_data(
    symbol="NIFTY",
    spot=22500.0,
    spot_chg=0.45,
    pcr=1.25,
    ce_vol=600000,
    pe_vol=100000,
    ce_chg_oi=-20000,
    pe_chg_oi=80000,
    data_status="LIVE",
    price_history=None
) -> dict:
    step = 50
    strikes = [22400, 22450, 22500, 22550, 22600]
    chain = []
    for s in strikes:
        chain.append({
            "strike": s,
            "ce_ltp": 100.0,
            "ce_bid": 99.5,
            "ce_ask": 100.5,
            "pe_ltp": 50.0,
            "pe_bid": 49.5,
            "pe_ask": 50.5,
            "ce_oi": 100000,
            "pe_oi": int(100000 * pcr),
            "ce_change_oi": ce_chg_oi,
            "pe_change_oi": pe_chg_oi,
            "ce_volume": ce_vol,
            "pe_volume": pe_vol,
            "ce_iv": 16.0,
            "pe_iv": 16.5,
        })

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


def test_1_strong_directional_move_at_12_15_generates_trade(monkeypatch):
    """1. Strong directional move at 12:15 can still generate a trade (no 11:45-13:00 midday ban)."""
    t_1215 = datetime(2026, 10, 5, 12, 15, tzinfo=IST)
    orig_filter = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(
        options_engine,
        "evaluate_iv_and_time_filter",
        lambda atm_iv, now_dt=None: orig_filter(atm_iv, t_1215),
    )

    recovery_history = [22450.0, 22530.0, 22485.0, 22515.0]
    data = _make_chain_data(
        spot=22515.0,
        spot_chg=0.45,
        pcr=1.30,
        ce_vol=700000,
        pe_vol=150000,
        ce_chg_oi=-25000,
        pe_chg_oi=90000,
        price_history=recovery_history,
    )
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["setup_type"] == "PULLBACK_RECOVERY"


def test_2_sideways_choppy_market_at_10_30_generates_wait(monkeypatch):
    """2. Sideways/choppy market at 10:30 generates WAIT with LOW_EXPANSION_OR_CHOP."""
    t_1030 = datetime(2026, 10, 5, 10, 30, tzinfo=IST)
    orig_filter = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(
        options_engine,
        "evaluate_iv_and_time_filter",
        lambda atm_iv, now_dt=None: orig_filter(atm_iv, t_1030),
    )

    # Choppy, oscillating price history with whipsaws and narrow range
    choppy_history = [22500.0, 22503.0, 22498.0, 22502.5, 22499.0, 22501.0]
    data = _make_chain_data(
        spot=22501.0,
        spot_chg=0.01,
        pcr=1.05,
        ce_vol=200000,
        pe_vol=200000,
        ce_chg_oi=5000,
        pe_chg_oi=5000,
        price_history=choppy_history,
    )
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["reason"] == "LOW_EXPANSION_OR_CHOP"


def test_3_pullback_recovery_strong_expansion_generates_buy(monkeypatch):
    """3. Pullback + recovery + strong expansion can generate BUY."""
    t_1030 = datetime(2026, 10, 5, 10, 30, tzinfo=IST)
    orig_filter = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(
        options_engine,
        "evaluate_iv_and_time_filter",
        lambda atm_iv, now_dt=None: orig_filter(atm_iv, t_1030),
    )

    # Bullish: new high, pullback to trough, followed by strong upward recovery & follow-through
    bullish_expansion_history = [100.0, 108.0, 115.0, 110.0, 112.5, 114.0]
    data = _make_chain_data(
        spot=114.0,
        spot_chg=0.40,
        pcr=1.25,
        ce_vol=600000,
        pe_vol=100000,
        ce_chg_oi=-20000,
        pe_chg_oi=80000,
        price_history=bullish_expansion_history,
    )
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["setup_type"] == "PULLBACK_RECOVERY"


def test_4_fresh_high_low_without_pullback_generates_wait(monkeypatch):
    """4. Fresh high/low without pullback generates WAIT."""
    t_1030 = datetime(2026, 10, 5, 10, 30, tzinfo=IST)
    orig_filter = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(
        options_engine,
        "evaluate_iv_and_time_filter",
        lambda atm_iv, now_dt=None: orig_filter(atm_iv, t_1030),
    )

    # Monotonic run to fresh high without pullback
    fresh_high_history = [100.0, 105.0, 110.0, 115.0]
    data = _make_chain_data(
        spot=115.0,
        spot_chg=0.50,
        pcr=1.30,
        ce_vol=800000,
        pe_vol=150000,
        ce_chg_oi=-30000,
        pe_chg_oi=100000,
        price_history=fresh_high_history,
    )
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["reason"] == "FRESH_HIGH_NO_PULLBACK"


def test_5_existing_active_trade_prevents_entry(monkeypatch):
    """5. Existing active trade prevents another entry."""
    t_1030 = datetime(2026, 10, 5, 10, 30, tzinfo=IST)
    orig_filter = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(
        options_engine,
        "evaluate_iv_and_time_filter",
        lambda atm_iv, now_dt=None: orig_filter(atm_iv, t_1030),
    )

    recovery_history = [22450.0, 22530.0, 22485.0, 22515.0]
    data = _make_chain_data(
        spot=22515.0,
        spot_chg=0.45,
        pcr=1.30,
        ce_vol=700000,
        pe_vol=150000,
        ce_chg_oi=-25000,
        pe_chg_oi=90000,
        price_history=recovery_history,
    )
    res = analyze_option_desk(data, has_active_trade=True)
    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["reason"] == "ACTIVE TRADE EXISTS"
    assert opt["active_trade_exists"] is True
