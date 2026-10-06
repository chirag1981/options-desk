"""
tests/test_time_window_and_oi_change.py
1. Entries are blocked outside the 09:30-14:00 IST window (gate, not just score).
2. Change in OI is derived from real data: broker 'poi' if present, else first OI seen today.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import options_engine
from app.services import fyers_options_service as fos

IST = ZoneInfo("Asia/Kolkata")


def _live_chain() -> dict:
    return {
        "symbol": "NIFTY",
        "spot_price": 22520.0,
        "spot_change_pct": 0.40,
        "strike_step": 50,
        "data_status": "LIVE",
        "chain": [
            {"strike": s, "ce_ltp": 100.0, "ce_bid": 99.5, "ce_ask": 100.5,
             "pe_ltp": 90.0, "pe_bid": 89.5, "pe_ask": 90.5,
             "ce_oi": 100000, "pe_oi": 150000, "ce_change_oi": 0, "pe_change_oi": 0,
             "ce_volume": 200000, "pe_volume": 100000}
            for s in (22400, 22450, 22500, 22550, 22600)
        ],
    }


def test_entry_blocked_after_1400(monkeypatch):
    original = options_engine.evaluate_iv_and_time_filter.__defaults__  # noqa: F841
    late = datetime(2026, 10, 5, 14, 30, tzinfo=IST)
    real_filter = fos  # placeholder to keep import used
    from app.services.options_engine import evaluate_iv_and_time_filter as _unused  # noqa: F401

    base = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(options_engine, "evaluate_iv_and_time_filter",
                        lambda atm_iv, now_dt=None: base(atm_iv, late))
    res = options_engine.analyze_option_desk(_live_chain())
    assert res["option_buying"]["decision"] == "WAIT"
    assert "Outside entry window" in res["option_buying"]["reason"]


def test_entry_blocked_before_0930(monkeypatch):
    early = datetime(2026, 10, 5, 9, 20, tzinfo=IST)
    base = options_engine.evaluate_iv_and_time_filter
    monkeypatch.setattr(options_engine, "evaluate_iv_and_time_filter",
                        lambda atm_iv, now_dt=None: base(atm_iv, early))
    res = options_engine.analyze_option_desk(_live_chain())
    assert res["option_buying"]["decision"] == "WAIT"
    assert "Outside entry window" in res["option_buying"]["reason"]


def test_change_in_oi_uses_broker_poi_when_present():
    assert fos._change_in_oi({"tsym": "X", "oi": "1500", "poi": "1000"}) == 500


def test_change_in_oi_uses_first_seen_baseline():
    q = {"tsym": "NIFTY06OCT26C22500", "oi": "1000000"}
    assert fos._change_in_oi(q) == 0                       # first sighting sets baseline
    assert fos._change_in_oi({**q, "oi": "1060000"}) == 60000
    assert fos._change_in_oi({**q, "oi": "940000"}) == -60000


def test_change_in_oi_baseline_survives_restart():
    q = {"tsym": "NIFTY06OCT26P22500", "oi": "500000"}
    fos._change_in_oi(q)
    fos._OI_BASELINE.update({"date": None, "oi": {}})      # simulate process restart
    assert fos._change_in_oi({**q, "oi": "550000"}) == 50000


def test_change_in_oi_missing_quote_is_zero():
    assert fos._change_in_oi(None) == 0
    assert fos._change_in_oi({"oi": "0"}) == 0
