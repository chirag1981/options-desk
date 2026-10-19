"""
tests/test_price_structure_breakout.py — Verification of Phase 2 & 3 Breakout-First Strategy

Covers:
1. Completed candle close above resistance + buffer confirms breakout -> BUY CE.
2. Intrabar spike above resistance with completed candle close below fails closed to WAIT (AWAITING_CANDLE_CLOSE).
3. Bearish completed candle close below support - buffer confirms breakdown -> BUY PE.
4. Intrabar dip below support with completed candle close above fails closed to WAIT (AWAITING_CANDLE_CLOSE).
5. Scheduler deduplication: Multiple scheduler polls within the same candle timestamp do NOT increment confirmation count.
6. Range-bound consolidation returns WAIT (RANGE_BOUND).
7. DATA_UNAVAILABLE fails closed to WAIT.
8. Underlying technical invalidation vs option stop loss (8-pt max risk cap, 3-pt min floor).
9. Strict paper trading invariant: no live broker orders are placed.
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

from app.services import options_engine
from app.services.options_engine import (
    analyze_option_desk,
    classify_market_regime,
    identify_price_structure_levels,
    evaluate_candle_breakout,
    ENGINE_CONFIG,
)
from app.services.options_scheduler import (
    update_pending_entry_confirmation,
    reset_pending_entry_confirmation,
    _PENDING_ENTRY_CONFIRMATIONS,
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


def _make_breakout_payload(
    spot: float = 22550.0,
    spot_chg: float = 0.35,
    today_high: float = 22560.0,
    today_low: float = 22400.0,
    candles: list[dict] | None = None,
    is_ce: bool = True,
    data_status: str = "LIVE",
) -> dict:
    step = 50
    strikes = [22450, 22500, 22550, 22600, 22650]
    chain = []
    for s in strikes:
        chain.append({
            "strike": s,
            "ce_ltp": 95.0,
            "ce_bid": 94.5,
            "ce_ask": 95.5,
            "pe_ltp": 65.0,
            "pe_bid": 64.5,
            "pe_ask": 65.5,
            "ce_oi": 80000 if is_ce else 150000,
            "pe_oi": 150000 if is_ce else 80000,
            "ce_change_oi": -25000 if is_ce else 40000,
            "pe_change_oi": 60000 if is_ce else -25000,
            "ce_volume": 600000 if is_ce else 150000,
            "pe_volume": 150000 if is_ce else 600000,
            "ce_iv": 16.5,
            "pe_iv": 17.0,
            "ce_delta": 0.52,
            "pe_delta": -0.48,
        })

    return {
        "symbol": "NIFTY",
        "spot_price": spot,
        "spot_change_pct": spot_chg,
        "high_price": today_high,
        "low_price": today_low,
        "strike_step": step,
        "data_status": data_status,
        "chain": chain,
        "underlying_candles": candles,
    }


def test_completed_candle_close_above_resistance_confirms_buy_ce():
    """1. Completed candle close above resistance + buffer confirms BUY CE."""
    # Prior swing high is 22520.0
    # Candidate candle closed at 22525.0 (> 22520 + 3 buffer = 22523)
    candles = [
        {"timestamp": 1000, "open": 22480, "high": 22510, "low": 22475, "close": 22505, "is_completed": True},
        {"timestamp": 1300, "open": 22505, "high": 22520, "low": 22495, "close": 22515, "is_completed": True},
        {"timestamp": 1600, "open": 22515, "high": 22528, "low": 22510, "close": 22526, "is_completed": True},
    ]
    data = _make_breakout_payload(spot=22526.0, spot_chg=0.30, today_high=22530.0, today_low=22450.0, candles=candles, is_ce=True)
    res = analyze_option_desk(data, has_active_trade=False)

    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert opt["setup_type"] == "TRENDING_BREAKOUT"
    assert "candle_breakout" in opt
    assert opt["candle_breakout"]["is_bull_break"] is True


def test_intrabar_test_without_candle_close_fails_closed_to_wait():
    """2. Intrabar spike above resistance with candle close below fails closed to WAIT (AWAITING_CANDLE_CLOSE)."""
    # Prior swing high is 22520.0
    # Candidate candle spiked to 22525.0 (intrabar test) but closed at 22516.0 (< 22520 + 3 = 22523)
    candles = [
        {"timestamp": 1000, "open": 22480, "high": 22510, "low": 22475, "close": 22505, "is_completed": True},
        {"timestamp": 1300, "open": 22505, "high": 22520, "low": 22495, "close": 22515, "is_completed": True},
        {"timestamp": 1600, "open": 22515, "high": 22525, "low": 22508, "close": 22516, "is_completed": True},
    ]
    data = _make_breakout_payload(spot=22524.0, spot_chg=0.25, today_high=22530.0, today_low=22450.0, candles=candles, is_ce=True)
    res = analyze_option_desk(data, has_active_trade=False)

    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "AWAITING_CANDLE_CLOSE" in opt["reason"]


def test_completed_candle_close_below_support_confirms_buy_pe():
    """3. Completed candle close below support - buffer confirms BUY PE."""
    # Prior swing low is 22480.0
    # Candidate candle closed at 22474.0 (< 22480 - 3 buffer = 22477)
    candles = [
        {"timestamp": 1000, "open": 22520, "high": 22525, "low": 22490, "close": 22495, "is_completed": True},
        {"timestamp": 1300, "open": 22495, "high": 22505, "low": 22480, "close": 22485, "is_completed": True},
        {"timestamp": 1600, "open": 22485, "high": 22490, "low": 22470, "close": 22474, "is_completed": True},
    ]
    data = _make_breakout_payload(spot=22474.0, spot_chg=-0.35, today_high=22550.0, today_low=22470.0, candles=candles, is_ce=False)
    res = analyze_option_desk(data, has_active_trade=False)

    opt = res["option_buying"]
    assert opt["decision"] == "BUY PE"
    assert opt["setup_type"] == "TRENDING_BREAKDOWN"
    assert opt["candle_breakout"]["is_bear_break"] is True


def test_intrabar_test_below_support_without_close_fails_closed_to_wait():
    """4. Intrabar dip below support with candle close above fails closed to WAIT (AWAITING_CANDLE_CLOSE)."""
    # Prior swing low is 22480.0
    # Candidate candle dipped to 22475.0 (intrabar dip) but closed at 22483.0 (> 22477)
    candles = [
        {"timestamp": 1000, "open": 22520, "high": 22525, "low": 22490, "close": 22495, "is_completed": True},
        {"timestamp": 1300, "open": 22495, "high": 22505, "low": 22480, "close": 22485, "is_completed": True},
        {"timestamp": 1600, "open": 22485, "high": 22490, "low": 22475, "close": 22483, "is_completed": True},
    ]
    data = _make_breakout_payload(spot=22478.0, spot_chg=-0.25, today_high=22550.0, today_low=22475.0, candles=candles, is_ce=False)
    res = analyze_option_desk(data, has_active_trade=False)

    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "AWAITING_CANDLE_CLOSE" in opt["reason"]


def test_scheduler_deduplication_prevents_multiple_polls_on_same_candle():
    """5. Multiple scheduler polls on the same candle timestamp do NOT increment confirmation counter."""
    sym = "NIFTY_TEST"
    reset_pending_entry_confirmation(sym)
    now_dt = datetime.now(IST)

    # First poll at candle timestamp 1700000000
    cnt1, confirmed1 = update_pending_entry_confirmation(
        symbol=sym, candidate_side="CE", req_confs=2, now_dt=now_dt, candle_ts=1700000000
    )
    assert cnt1 == 1
    assert confirmed1 is False

    # Second poll 10 seconds later, still on candle timestamp 1700000000 (same candle)
    cnt2, confirmed2 = update_pending_entry_confirmation(
        symbol=sym, candidate_side="CE", req_confs=2, now_dt=now_dt, candle_ts=1700000000
    )
    assert cnt2 == 1  # Deduplicated! Must NOT increment to 2
    assert confirmed2 is False

    # Third poll on a NEW completed candle timestamp 1700000300
    cnt3, confirmed3 = update_pending_entry_confirmation(
        symbol=sym, candidate_side="CE", req_confs=2, now_dt=now_dt, candle_ts=1700000300
    )
    assert cnt3 == 2  # Increments only on new candle!
    assert confirmed3 is True


def test_range_bound_consolidation_returns_wait():
    """6. Range-bound chop returns WAIT (RANGE_BOUND)."""
    # Flat oscillating candles
    candles = [
        {"timestamp": 1000, "open": 22500, "high": 22505, "low": 22495, "close": 22501, "is_completed": True},
        {"timestamp": 1300, "open": 22501, "high": 22504, "low": 22496, "close": 22499, "is_completed": True},
        {"timestamp": 1600, "open": 22499, "high": 22503, "low": 22497, "close": 22500, "is_completed": True},
    ]
    data = _make_breakout_payload(spot=22500.0, spot_chg=0.01, today_high=22510.0, today_low=22490.0, candles=candles, is_ce=True)
    res = analyze_option_desk(data, has_active_trade=False)

    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert opt["market_regime"] == "RANGE_BOUND"


def test_data_unavailable_fails_closed_to_wait():
    """7. DATA_UNAVAILABLE status fails closed to WAIT."""
    data = _make_breakout_payload(spot=22526.0, data_status="DATA_UNAVAILABLE")
    res = analyze_option_desk(data, has_active_trade=False)

    opt = res["option_buying"]
    assert opt["decision"] == "WAIT"
    assert "DATA_UNAVAILABLE" in res["meta"]["data_status"]


def test_technical_invalidation_and_option_risk_cap():
    """8. Underlying technical invalidation vs option stop-loss (8-pt max risk cap, 3-pt min floor)."""
    candles = [
        {"timestamp": 1000, "open": 22480, "high": 22510, "low": 22475, "close": 22505, "is_completed": True},
        {"timestamp": 1300, "open": 22505, "high": 22520, "low": 22495, "close": 22515, "is_completed": True},
        {"timestamp": 1600, "open": 22515, "high": 22530, "low": 22510, "close": 22528, "is_completed": True},
    ]
    data = _make_breakout_payload(spot=22528.0, spot_chg=0.30, today_high=22530.0, today_low=22450.0, candles=candles, is_ce=True)
    res = analyze_option_desk(data, has_active_trade=False)

    plan = res["option_buying"]["trade_plan"]
    assert plan is not None
    # Risk points strictly bounded between 3.0 and 8.0 points
    assert 3.0 <= plan["risk_pts"] <= 8.0
    assert plan["stop_loss"] == pytest.approx(plan["entry_price"] - plan["risk_pts"], abs=0.05)
    # Underlying invalidation is recorded
    assert "spot_invalidation" in plan


def test_paper_trading_invariant_strictly_enforced():
    """9. Strict paper trading invariant: no live broker orders are placed."""
    with patch("fyers_apiv3.fyersModel.FyersModel.place_order") as mock_place_order:
        candles = [
            {"timestamp": 1000, "open": 22480, "high": 22510, "low": 22475, "close": 22505, "is_completed": True},
            {"timestamp": 1300, "open": 22505, "high": 22520, "low": 22495, "close": 22515, "is_completed": True},
            {"timestamp": 1600, "open": 22515, "high": 22530, "low": 22510, "close": 22528, "is_completed": True},
        ]
        data = _make_breakout_payload(spot=22528.0, spot_chg=0.30, candles=candles, is_ce=True)
        res = analyze_option_desk(data, has_active_trade=False)

        assert res["option_buying"]["decision"] == "BUY CE"
        mock_place_order.assert_not_called()
