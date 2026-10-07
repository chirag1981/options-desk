"""
tests/test_trending_oi.py — Unit & Integration Tests for Trending OI (Oi Pulse Specification)
Verifies:
  1. Automatic selection of 5 strikes up and 5 strikes down from ATM (11 strikes total).
  2. Oi Pulse Difference % and Sentiment formulas (Call-dominant negative, Put-dominant positive).
  3. Strength capsule thresholds (40%+ with 2+ dots rule, <30% weak/grayed out, >=60% 3 dots).
  4. Time-series metrics (Day H/L Break, Day High/Low Diff in OI, Direction of change, Net PCR).
  5. Persistence and retrieval via get_trending_oi_timeseries and API endpoint.
"""

import pytest
from app import create_app
from app.services.options_engine import compute_trending_oi_metrics
from app.services.options_signal_service import (
    record_trending_oi_snapshot,
    get_trending_oi_timeseries,
    init_signal_db,
    _get_db,
)


@pytest.fixture
def app():
    init_signal_db()
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_automatic_5_strikes_up_and_down_from_atm():
    """1. Verifies automatic selection of 5 strikes up and 5 strikes down from ATM (11 strikes total)."""
    spot = 24011.15
    step = 50.0
    atm = 24000.0  # round(24011.15 / 50) * 50

    # Build mock chain covering 20 strikes
    chain = []
    for strike in range(23500, 24550, 50):
        chain.append({
            "strike": float(strike),
            "ce_oi": 1000000,
            "pe_oi": 800000,
            "ce_change_oi": 50000,
            "pe_change_oi": 30000,
        })

    metrics = compute_trending_oi_metrics(chain, spot, step, atm, num_strikes_each_side=5)

    expected_strikes = [
        23750.0, 23800.0, 23850.0, 23900.0, 23950.0,
        24000.0,  # ATM
        24050.0, 24100.0, 24150.0, 24200.0, 24250.0
    ]

    assert metrics["num_strikes"] == 11
    assert metrics["selected_strikes"] == expected_strikes
    assert metrics["atm_strike"] == 24000.0


def test_oi_pulse_diff_pct_and_sentiment_formulas():
    """
    2. Tests exact Oi Pulse calculations matching video/screenshot:
       Row 14: Call Chg 8,30,32,065, Put Chg 3,92,77,235 -> Diff: -4,37,54,830, -53%, Bearish
       Row 18: Call Chg 5,41,68,735, Put Chg 6,08,50,990 -> Diff: +66,82,255, +11%, Bullish
    """
    atm = 24000.0
    step = 50.0

    # Test Case A: Bearish (Row 14 from video)
    chain_bearish = [
        {"strike": 24000.0, "ce_change_oi": 83032065, "pe_change_oi": 39277235}
    ]
    metrics_bearish = compute_trending_oi_metrics(chain_bearish, 23968.4, step, atm, num_strikes_each_side=0)
    assert metrics_bearish["diff_oi"] == -43754830
    assert metrics_bearish["diff_pct"] == -53
    assert metrics_bearish["sentiment"] == "Bearish"
    assert metrics_bearish["dots"] == 2
    assert metrics_bearish["strength_class"] == "strength-bearish"

    # Test Case B: Bullish (Row 18 from video)
    chain_bullish = [
        {"strike": 24000.0, "ce_change_oi": 54168735, "pe_change_oi": 60850990}
    ]
    metrics_bullish = compute_trending_oi_metrics(chain_bullish, 24061.2, step, atm, num_strikes_each_side=0)
    assert metrics_bullish["diff_oi"] == 6682255
    assert metrics_bullish["diff_pct"] == 11
    assert metrics_bullish["sentiment"] == "Bullish"
    assert metrics_bullish["dots"] == 0  # < 30% has 0 dots
    assert metrics_bullish["strength_class"] == "strength-weak"


def test_strength_dots_and_capsule_thresholds():
    """
    3. Tests conviction dots threshold rules:
       - < 30%: 0 dots, weak
       - 30% - 39%: 1 dot, weak
       - 40% - 59%: 2 dots, active (>= 40% rule for trading conviction)
       - >= 60%: 3 dots, strong
    """
    atm = 24000.0
    step = 50.0

    # 1. < 30% (e.g. 20%) -> 0 dots
    c1 = [{"strike": 24000.0, "ce_change_oi": 1000000, "pe_change_oi": 800000}]
    m1 = compute_trending_oi_metrics(c1, 24000.0, step, atm, num_strikes_each_side=0)
    assert m1["diff_pct"] == -20
    assert m1["dots"] == 0
    assert m1["strength_class"] == "strength-weak"

    # 2. 35% -> 1 dot, weak
    c2 = [{"strike": 24000.0, "ce_change_oi": 1000000, "pe_change_oi": 650000}]
    m2 = compute_trending_oi_metrics(c2, 24000.0, step, atm, num_strikes_each_side=0)
    assert m2["diff_pct"] == -35
    assert m2["dots"] == 1
    assert m2["strength_class"] == "strength-weak"

    # 3. 43% -> 2 dots, active bearish
    c3 = [{"strike": 24000.0, "ce_change_oi": 1000000, "pe_change_oi": 570000}]
    m3 = compute_trending_oi_metrics(c3, 24000.0, step, atm, num_strikes_each_side=0)
    assert m3["diff_pct"] == -43
    assert m3["dots"] == 2
    assert m3["strength_class"] == "strength-bearish"

    # 4. 69% -> 3 dots, active bearish
    c4 = [{"strike": 24000.0, "ce_change_oi": 1000000, "pe_change_oi": 310000}]
    m4 = compute_trending_oi_metrics(c4, 24000.0, step, atm, num_strikes_each_side=0)
    assert m4["diff_pct"] == -69
    assert m4["dots"] == 3
    assert m4["strength_class"] == "strength-bearish"


def test_get_trending_oi_timeseries_structure_and_columns():
    """4. Tests that get_trending_oi_timeseries returns all 14 columns matching Oi Pulse specification."""
    ts = get_trending_oi_timeseries("NIFTY", interval_minutes=15, spot_price=24000.0, strike_step=50.0)

    assert "rows" in ts
    assert len(ts["rows"]) > 0
    assert "selected_strikes" in ts
    assert len(ts["selected_strikes"]) == 11

    # Verify first row contains all fields including institutional build-up & leg activities
    row0 = ts["rows"][0]
    required_fields = [
        "date", "time", "ltp", "day_hl_break", "ce_change_oi", "pe_change_oi",
        "diff_oi", "strength_pct", "strength_dots", "strength_class",
        "direction_arrow", "direction_color", "chng_in_direction",
        "direction_chng_pct", "net_pcr", "day_hl_diff_oi", "sentiment",
        "buildup_type", "buildup_label", "buildup_class", "ce_action", "pe_action", "sub_activity"
    ]
    for field in required_fields:
        assert field in row0, f"Missing field: {field}"

    # Verify time sorting: rows are in descending order (latest interval at top)
    if len(ts["rows"]) >= 2:
        t0 = ts["rows"][0]["time"]
        t1 = ts["rows"][1]["time"]
        assert t0 >= t1, f"Expected descending order: {t0} >= {t1}"


def test_buildup_classification_regimes():
    """5. Tests institutional 4-state build-up classification on sample time series."""
    ts = get_trending_oi_timeseries("NIFTY", interval_minutes=5, spot_price=22600.0, strike_step=50.0)
    for r in ts["rows"]:
        assert r["buildup_type"] in ("LONG_BUILDUP", "SHORT_COVERING", "SHORT_BUILDUP", "LONG_UNWINDING", "BULLISH_SUPPORT", "BEARISH_RESIST", "CONSOLIDATION", "NEUTRAL")
        assert len(r["buildup_label"]) > 0
        assert "buildup-" in r["buildup_class"]
        assert len(r["sub_activity"]) > 0


def test_api_trending_oi_endpoint(client):
    """6. Tests GET /api/options-desk/trending-oi endpoint."""
    # Test default interval is 5 minutes when not passed
    resp_def = client.get("/api/options-desk/trending-oi?symbol=NIFTY")
    assert resp_def.status_code == 200
    data_def = resp_def.get_json()
    assert data_def["success"] is True
    assert data_def["data"]["interval_minutes"] == 5

    # Test explicit interval=15
    resp = client.get("/api/options-desk/trending-oi?symbol=NIFTY&interval=15")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "data" in data
    assert data["data"]["symbol"] == "NIFTY"
    assert data["data"]["interval_minutes"] == 15
    assert len(data["data"]["rows"]) > 0
    assert "buildup_label" in data["data"]["rows"][0]
