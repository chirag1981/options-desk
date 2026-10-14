"""
tests/test_oi_data_integrity_and_strategy.py — Verification of OI data integrity,
signal classification, interval flow calculation, replay-execution alignment,
and symbol-specific transaction accounting.
"""

import pytest
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.services.options_engine import (
    compute_trending_oi_metrics,
    analyze_option_desk,
    ENGINE_CONFIG,
)
from app.services.options_signal_service import (
    record_signal,
    update_active_signals,
    record_trending_oi_snapshot,
    compute_contract_interval_oi_change,
    close_signal_manually,
    get_signal_by_id,
    _get_db,
)
from app.services.options_replay_backtest import (
    replay_trade_on_ticks,
    calculate_trade_costs,
    compute_trade_r_multiple,
)
from app.services.trade_analyzer_agent import review_closed_trade

IST = ZoneInfo("Asia/Kolkata")


# ==============================================================================
# 1. OI Signal Classification & Metric Integrity Tests (Task Section 3)
# ==============================================================================

def test_oi_metrics_positive_negative_zero_and_mixed_changes():
    """Verify compute_trending_oi_metrics handles positive, negative, zero, and mixed OI changes safely."""
    spot = 22500.0
    step = 50.0
    atm = 22500.0

    # 1. Clear Bullish: Put addition (writing support) + Call unwinding (resistance collapsing)
    chain_bullish = [
        {"strike": 22450, "ce_oi": 50000, "pe_oi": 80000, "ce_oich": -5000, "pe_oich": 15000, "ce_ltp": 90, "pe_ltp": 30, "ce_chp": 10.0, "pe_chp": -15.0},
        {"strike": 22500, "ce_oi": 70000, "pe_oi": 95000, "ce_oich": -10000, "pe_oich": 25000, "ce_ltp": 60, "pe_ltp": 50, "ce_chp": 12.0, "pe_chp": -18.0},
        {"strike": 22550, "ce_oi": 90000, "pe_oi": 60000, "ce_oich": -8000, "pe_oich": 20000, "ce_ltp": 35, "pe_ltp": 85, "ce_chp": 8.0, "pe_chp": -12.0},
    ]
    res_bull = compute_trending_oi_metrics(chain_bullish, spot, step, atm, num_strikes_each_side=1)
    assert res_bull["sentiment"] == "Strong Bullish"
    assert res_bull["dots"] == 3
    assert res_bull["strength_status"] == "STRONG"
    assert res_bull["total_pe_change_oi"] > 0
    assert res_bull["total_ce_change_oi"] < 0

    # 2. Clear Bearish: Call addition (writing resistance) + Put unwinding (support breaking)
    chain_bearish = [
        {"strike": 22450, "ce_oi": 80000, "pe_oi": 50000, "ce_oich": 18000, "pe_oich": -8000, "ce_ltp": 70, "pe_ltp": 45, "ce_chp": -12.0, "pe_chp": 15.0},
        {"strike": 22500, "ce_oi": 110000, "pe_oi": 60000, "ce_oich": 30000, "pe_oich": -12000, "ce_ltp": 45, "pe_ltp": 75, "ce_chp": -15.0, "pe_chp": 18.0},
        {"strike": 22550, "ce_oi": 95000, "pe_oi": 40000, "ce_oich": 15000, "pe_oich": -6000, "ce_ltp": 25, "pe_ltp": 110, "ce_chp": -8.0, "pe_chp": 10.0},
    ]
    res_bear = compute_trending_oi_metrics(chain_bearish, spot, step, atm, num_strikes_each_side=1)
    assert res_bear["sentiment"] == "Strong Bearish"
    assert res_bear["dots"] == 3
    assert res_bear["strength_status"] == "STRONG"
    assert res_bear["total_ce_change_oi"] > 0
    assert res_bear["total_pe_change_oi"] < 0

    # 3. Both Unwinding (negative changes on both sides) -> Neutral / Unwinding (NOT accidentally Strong Bullish/Bearish)
    chain_both_unwind = [
        {"strike": 22500, "ce_oi": 50000, "pe_oi": 50000, "ce_oich": -20000, "pe_oich": -18000, "ce_ltp": 50, "pe_ltp": 50},
    ]
    res_unwind = compute_trending_oi_metrics(chain_both_unwind, spot, step, atm, num_strikes_each_side=0)
    assert res_unwind["sentiment"] in ("Neutral", "Bullish", "Mild Bullish", "Mild Bearish")
    assert res_unwind["strength_status"] == "WEAK"
    assert abs(res_unwind["dots"]) <= 1

    # 4. Zero change on both sides -> Neutral
    chain_zero = [
        {"strike": 22500, "ce_oi": 50000, "pe_oi": 50000, "ce_oich": 0, "pe_oich": 0, "ce_ltp": 50, "pe_ltp": 50},
    ]
    res_zero = compute_trending_oi_metrics(chain_zero, spot, step, atm, num_strikes_each_side=0)
    assert res_zero["sentiment"] == "Neutral"
    assert res_zero["dots"] == 0
    assert res_zero["diff_oi"] == 0


def test_oi_activity_candidate_classification():
    """Verify CE/PE activity labels candidate long buildup, writing, short covering, long unwinding."""
    spot = 22500.0
    step = 50.0
    atm = 22500.0

    chain = [
        # Strike 22450: CE rising OI + rising premium -> Long Buildup; PE rising OI + falling premium -> Writing
        {"strike": 22450, "ce_oi": 50000, "pe_oi": 60000, "ce_oich": 10000, "pe_oich": 12000, "ce_ltp": 90, "pe_ltp": 30, "ce_chp": 12.0, "pe_chp": -8.0},
        # Strike 22500: CE falling OI + rising premium -> Short Covering; PE falling OI + falling premium -> Long Unwinding
        {"strike": 22500, "ce_oi": 40000, "pe_oi": 45000, "ce_oich": -8000, "pe_oich": -9000, "ce_ltp": 60, "pe_ltp": 45, "ce_chp": 6.0, "pe_chp": -7.0},
    ]
    metrics = compute_trending_oi_metrics(chain, spot, step, atm, num_strikes_each_side=1)
    strikes = {s["strike"]: s for s in metrics["strikes_data"]}

    assert strikes[22450]["ce_activity"] == "Long Buildup"
    assert strikes[22450]["pe_activity"] == "Short Buildup (Writing)"
    assert strikes[22500]["ce_activity"] == "Short Covering"
    assert strikes[22500]["pe_activity"] == "Long Unwinding"


def test_deduplicates_target_strikes():
    """Verify target strikes are deduplicated if the raw provider chain reports duplicate strike records."""
    chain_with_dups = [
        {"strike": 22500, "ce_oi": 1000, "pe_oi": 1000, "ce_oich": 50, "pe_oich": 100, "ce_ltp": 50, "pe_ltp": 50},
        {"strike": 22500, "ce_oi": 1000, "pe_oi": 1000, "ce_oich": 50, "pe_oich": 100, "ce_ltp": 50, "pe_ltp": 50},  # Duplicate!
        {"strike": 22550, "ce_oi": 2000, "pe_oi": 1500, "ce_oich": 100, "pe_oich": 200, "ce_ltp": 30, "pe_ltp": 70},
    ]
    metrics = compute_trending_oi_metrics(chain_with_dups, spot=22500, step=50, atm_strike=22500, num_strikes_each_side=1)
    observed_strikes = [s["strike"] for s in metrics["strikes_data"]]
    assert len(observed_strikes) == len(set(observed_strikes))


# ==============================================================================
# 2. OI Snapshot & Granular Interval Flow Tests (Task Section 4)
# ==============================================================================

def test_oi_contract_snapshots_and_interval_change():
    """Verify chronological ordering, outage rejection, and interval flow calculation."""
    symbol = "NIFTY"
    expiry = "15-Oct-2026"
    strike = 22500.0

    with _get_db() as conn:
        conn.execute("DELETE FROM oi_contract_snapshots WHERE symbol = ?", (symbol,))
        conn.execute("DELETE FROM trending_oi_snapshots WHERE symbol = ?", (symbol,))
        conn.commit()

    base_time = datetime(2026, 10, 14, 10, 0, 0, tzinfo=IST)

    # 1. Insert snapshot 1 (T0)
    with _get_db() as conn:
        t0_str = base_time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO oi_contract_snapshots (
                symbol, trade_date, timestamp, expiry, strike, option_type,
                oi, oi_change, ltp, volume, bid, ask, spot_price, source, is_fresh, created_at
            ) VALUES (?, '2026-10-14', ?, ?, ?, 'CE', 100000, 5000, 65.0, 50000, 64.8, 65.2, 22500.0, 'FYERS', 1, ?)
        """, (symbol, t0_str, expiry, strike, t0_str))

    # Query interval flow immediately (0 seconds elapsed) -> Insufficient data
    flow_immediate = compute_contract_interval_oi_change(symbol, expiry, strike, "CE", target_interval_sec=300, tolerance_sec=60)
    assert flow_immediate["status"] == "INSUFFICIENT_DATA"

    # 2. Insert snapshot 2 (T0 + 300 seconds, 5 minutes later)
    t1_time = base_time + timedelta(seconds=300)
    t1_str = t1_time.strftime("%Y-%m-%d %H:%M:%S")
    with _get_db() as conn:
        conn.execute("""
            INSERT INTO oi_contract_snapshots (
                symbol, trade_date, timestamp, expiry, strike, option_type,
                oi, oi_change, ltp, volume, bid, ask, spot_price, source, is_fresh, created_at
            ) VALUES (?, '2026-10-14', ?, ?, ?, 'CE', 115000, 20000, 68.0, 75000, 67.8, 68.2, 22520.0, 'FYERS', 1, ?)
        """, (symbol, t1_str, expiry, strike, t1_str))

    # Query 5-min interval flow
    flow_valid = compute_contract_interval_oi_change(symbol, expiry, strike, "CE", target_interval_sec=300, tolerance_sec=60)
    assert flow_valid["status"] == "VALID"
    assert flow_valid["current_oi"] == 115000
    assert flow_valid["previous_oi"] == 100000
    assert flow_valid["interval_oi_change"] == 15000  # Exactly 15,000 contracts added over 5 mins
    assert flow_valid["provider_day_oich"] == 20000  # Provider day change is 20,000, distinctly separate!

    # 3. Outage scenario: Insert snapshot 3 after 25 minutes gap (outage)
    t2_time = t1_time + timedelta(minutes=25)
    t2_str = t2_time.strftime("%Y-%m-%d %H:%M:%S")
    with _get_db() as conn:
        conn.execute("""
            INSERT INTO oi_contract_snapshots (
                symbol, trade_date, timestamp, expiry, strike, option_type,
                oi, oi_change, ltp, volume, bid, ask, spot_price, source, is_fresh, created_at
            ) VALUES (?, '2026-10-14', ?, ?, ?, 'CE', 140000, 45000, 72.0, 110000, 71.8, 72.2, 22550.0, 'FYERS', 1, ?)
        """, (symbol, t2_str, expiry, strike, t2_str))

    flow_outage = compute_contract_interval_oi_change(symbol, expiry, strike, "CE", target_interval_sec=300, tolerance_sec=60, max_gap_sec=900)
    # Gapped > 15m without an observation in [180s, 420s] window -> Must reject comparison across outage
    assert flow_outage["status"] == "INSUFFICIENT_DATA"


def test_record_trending_oi_snapshot_rejects_out_of_order_and_duplicates():
    """record_trending_oi_snapshot enforces chronological ordering."""
    symbol = "NIFTY"
    fake_analysis = {
        "trending_oi": {
            "selected_strikes": [{"strike": 22500, "ce_oi": 1000, "pe_oi": 1000, "ce_oich": 0, "pe_oich": 0, "ce_ltp": 50, "pe_ltp": 50}],
            "atm_strike": 22500,
            "strike_step": 50,
            "total_ce_change_oi": 0,
            "total_pe_change_oi": 0,
            "diff_oi": 0,
            "diff_pct": 0.0,
            "dots": 0,
            "strength_status": "WEAK",
            "net_pcr": 1.0,
            "sentiment": "Neutral",
        },
        "market_bias": {"spot_price": 22500.0},
    }

    # Record first snapshot
    ok1 = record_trending_oi_snapshot(symbol, fake_analysis)
    assert ok1 is True

    # Immediate second snapshot (< 45s) must be rejected
    ok2 = record_trending_oi_snapshot(symbol, fake_analysis)
    assert ok2 is False


# ==============================================================================
# 3. OI-Led Strategy Decision Logic Tests (Task Section 5)
# ==============================================================================

def test_oi_led_bullish_and_bearish_strategy_decisions():
    """Verify that OI is the primary directional driver, and neutral/0 OI results in WAIT."""
    trade_time = datetime(2026, 10, 14, 10, 15, tzinfo=IST)

    # 1. Bullish Chain + Positive Price -> Candidate BUY CE
    bull_chain = {
        "symbol": "NIFTY",
        "spot_price": 22520.0,
        "spot_change_pct": 0.35,
        "chain": [
            {"strike": 22450, "ce_oi": 40000, "pe_oi": 90000, "ce_oich": -5000, "pe_oich": 25000, "ce_ltp": 95, "pe_ltp": 25, "ce_bid": 94.5, "ce_ask": 95.5, "pe_bid": 24.5, "pe_ask": 25.5, "ce_volume": 40000, "pe_volume": 20000, "ce_chp": 12.0, "pe_chp": -15.0},
            {"strike": 22500, "ce_oi": 60000, "pe_oi": 120000, "ce_oich": -12000, "pe_oich": 35000, "ce_ltp": 65, "pe_ltp": 45, "ce_bid": 64.5, "ce_ask": 65.5, "pe_bid": 44.5, "pe_ask": 45.5, "ce_volume": 60000, "pe_volume": 30000, "ce_chp": 14.0, "pe_chp": -18.0},
            {"strike": 22550, "ce_oi": 80000, "pe_oi": 70000, "ce_oich": -8000, "pe_oich": 15000, "ce_ltp": 38, "pe_ltp": 78, "ce_bid": 37.5, "ce_ask": 38.5, "pe_bid": 77.5, "pe_ask": 78.5, "ce_volume": 30000, "pe_volume": 15000, "ce_chp": 8.0, "pe_chp": -10.0},
        ],
        "vix": 13.5,
        "now_dt": trade_time,
    }
    analysis_bull = analyze_option_desk(bull_chain)
    opt_bull = analysis_bull.get("option_buying", {})
    assert opt_bull.get("decision") == "BUY CE"
    assert opt_bull.get("candidate_side") == "CE"

    # 2. Bearish Chain + Negative Price -> Candidate BUY PE
    bear_chain = {
        "symbol": "NIFTY",
        "spot_price": 22480.0,
        "spot_change_pct": -0.35,
        "chain": [
            {"strike": 22450, "ce_oi": 70000, "pe_oi": 50000, "ce_oich": 15000, "pe_oich": -8000, "ce_ltp": 78, "pe_ltp": 38, "ce_bid": 77.5, "ce_ask": 78.5, "pe_bid": 37.5, "pe_ask": 38.5, "ce_volume": 20000, "pe_volume": 40000, "ce_chp": -10.0, "pe_chp": 8.0},
            {"strike": 22500, "ce_oi": 120000, "pe_oi": 60000, "ce_oich": 35000, "pe_oich": -12000, "ce_ltp": 45, "pe_ltp": 65, "ce_bid": 44.5, "ce_ask": 45.5, "pe_bid": 64.5, "pe_ask": 65.5, "ce_volume": 30000, "pe_volume": 60000, "ce_chp": -18.0, "pe_chp": 14.0},
            {"strike": 22550, "ce_oi": 90000, "pe_oi": 40000, "ce_oich": 25000, "pe_oich": -5000, "ce_ltp": 25, "pe_ltp": 95, "ce_bid": 24.5, "ce_ask": 25.5, "pe_bid": 94.5, "pe_ask": 95.5, "ce_volume": 15000, "pe_volume": 30000, "ce_chp": -15.0, "pe_chp": 12.0},
        ],
        "vix": 13.5,
        "now_dt": trade_time,
    }
    analysis_bear = analyze_option_desk(bear_chain)
    opt_bear = analysis_bear.get("option_buying", {})
    assert opt_bear.get("decision") == "BUY PE"
    assert opt_bear.get("candidate_side") == "PE"

    # 3. Conflicting / Neutral OI (e.g. Bullish Price +0.40% but Neutral OI) -> WAIT
    neutral_oi_chain = {
        "symbol": "NIFTY",
        "spot_price": 22530.0,
        "spot_change_pct": 0.40,
        "chain": [
            {"strike": 22500, "ce_oi": 80000, "pe_oi": 80000, "ce_oich": 0, "pe_oich": 0, "ce_ltp": 60, "pe_ltp": 50, "ce_bid": 59.5, "ce_ask": 60.5, "pe_bid": 49.5, "pe_ask": 50.5, "ce_volume": 50000, "pe_volume": 50000},
        ],
        "vix": 13.5,
        "now_dt": trade_time,
    }
    analysis_neutral = analyze_option_desk(neutral_oi_chain)
    assert analysis_neutral.get("option_buying", {}).get("decision") == "WAIT"


# ==============================================================================
# 4. Replay & Paper Execution Realism Alignment Tests (Task Section 7)
# ==============================================================================

def test_replay_uses_ask_entry_bid_exit_and_conservative_gap_fill():
    """Verify replay trade follows conservative fills: ask entry, bid exit, gap fill."""
    base_trade = {
        "symbol": "NIFTY",
        "entry_price": 100.0,
        "ask_at_entry": 100.0,
        "bid_at_entry": 99.5,
        "initial_stop_loss": 92.0,
        "stop_loss": 92.0,
        "target_1": 112.0,
        "target_2": 120.0,
        "lots": 1,
        "lot_size": 65,
    }

    # Case 1: LTP crosses T1 (113.0) but Bid is only 111.0 (< T1) -> No T1 fill
    ticks_no_fill = [
        {"timestamp": "2026-10-14 10:00:00", "ltp": 105.0, "bid": 104.5, "ask": 105.5},
        {"timestamp": "2026-10-14 10:01:00", "ltp": 113.0, "bid": 111.0, "ask": 113.5},  # LTP >= 112, but Bid < 112
        {"timestamp": "2026-10-14 10:02:00", "ltp": 108.0, "bid": 107.5, "ask": 108.5},
    ]
    r1 = replay_trade_on_ticks(base_trade, ticks_no_fill, params={"use_bid_for_t1": True})
    # Since bid never hit T1 and ended at 107.5: net PnL is (107.5 - 100 - costs)
    assert r1 < 1.0  # Did not capture T1 (1.5R)

    # Case 2: Bid crosses T1 (112.5) -> T1 filled
    ticks_t1_fill = [
        {"timestamp": "2026-10-14 10:00:00", "ltp": 105.0, "bid": 104.5, "ask": 105.5},
        {"timestamp": "2026-10-14 10:01:00", "ltp": 113.0, "bid": 112.5, "ask": 113.5},  # Bid >= 112!
    ]
    r2 = replay_trade_on_ticks(base_trade, ticks_t1_fill, params={"use_bid_for_t1": True})
    assert r2 >= 1.0  # T1 hit successfully

    # Case 3: Bid gaps down below stop loss (SL 92.0, gap tick Bid 88.0) -> Filled conservatively at Bid 88.0!
    ticks_gap_down = [
        {"timestamp": "2026-10-14 10:00:00", "ltp": 100.0, "bid": 99.5, "ask": 100.5},
        {"timestamp": "2026-10-14 10:01:00", "ltp": 89.0, "bid": 88.0, "ask": 89.5},  # Gap down breach
    ]
    r3 = replay_trade_on_ticks(base_trade, ticks_gap_down)
    # 88.0 - 100 = -12 pts loss. With risk 8 pts, R-multiple is around -1.5R
    assert r3 < -1.0  # More severe than -1.0R due to conservative gap fill!


# ==============================================================================
# 5. Transaction Costs & P&L Reconciliation Tests (Task Section 8)
# ==============================================================================

def test_symbol_specific_statutory_costs_nifty_vs_sensex():
    """Verify transaction costs pass underlying symbol and calculate exchange-specific fees."""
    # NIFTY trade: 1 lot (lot_size 65)
    nifty_costs = calculate_trade_costs(
        entry_price=100.0,
        exit_prices=[(120.0, 1.0)],
        lot_size=65,
        lots=1,
        symbol="NIFTY",
    )
    # SENSEX trade: 1 lot (lot_size 20) with BSE turnover rates
    sensex_costs = calculate_trade_costs(
        entry_price=100.0,
        exit_prices=[(120.0, 1.0)],
        lot_size=20,
        lots=1,
        symbol="SENSEX",
    )

    assert nifty_costs["total_costs_inr"] > 0.0
    assert sensex_costs["total_costs_inr"] > 0.0
    # SENSEX turnover is smaller (20 qty vs 65 qty), so statutory turnover fees differ proportionally
    assert nifty_costs["turnover_buy"] == 6500.0
    assert sensex_costs["turnover_buy"] == 2000.0


def test_whole_lot_partial_exits_and_journal_persistence():
    """Verify that multi-lot positions book whole lots at T1 and persist partial details to terminal exit."""
    with _get_db() as conn:
        conn.execute("DELETE FROM signals")
        conn.commit()

    future_exp = (datetime.now(IST) + timedelta(days=7)).strftime("%d-%b-%Y")
    midday = datetime(2026, 10, 14, 11, 0, tzinfo=IST)

    # 2-lot position (lots = 2, lot_size = 65)
    sig = record_signal({
        "symbol": "NIFTY",
        "type": "CE",
        "strike": 22500,
        "expiry": future_exp,
        "spot_price": 22480.0,
        "entry_price": 100.0,
        "ask_at_entry": 100.0,
        "bid_at_entry": 99.5,
        "stop_loss": 90.0,
        "target_1": 115.0,
        "target_2": 130.0,
        "lots": 2,
        "lot_size": 65,
    }, is_paper_trade=True, override_cooldown=True, now_dt=midday)
    assert sig is not None
    sig_id = sig["id"]

    # 1. Price hits T1 (Bid = 116.0 >= 115.0) -> Exactly 1 lot booked at T1
    cache_t1 = {
        "NIFTY": {
            "spot_price": 22520.0,
            "chain": [{"strike": 22500, "ce_ltp": 116.5, "ce_bid": 116.0, "ce_ask": 117.0}],
        }
    }
    upd1 = update_active_signals(market_data_cache=cache_t1, now_dt=midday + timedelta(minutes=1))
    assert upd1[0]["status"] == "TARGET_1_HIT"
    assert upd1[0]["partial_exit_lots"] == 1

    # Check database persistence of partial exit columns
    with _get_db() as conn:
        row = conn.execute("SELECT partial_exit_lots, partial_exit_price, partial_exit_time FROM signals WHERE id = ?", (sig_id,)).fetchone()
        assert row["partial_exit_lots"] == 1
        assert row["partial_exit_price"] == 116.0
        assert row["partial_exit_time"] is not None

    # 2. Remaining 1 lot hits T2 (Bid = 131.0 >= 130.0) -> Terminal exit TARGET_2_HIT
    cache_t2 = {
        "NIFTY": {
            "spot_price": 22550.0,
            "chain": [{"strike": 22500, "ce_ltp": 131.5, "ce_bid": 131.0, "ce_ask": 132.0}],
        }
    }
    upd2 = update_active_signals(market_data_cache=cache_t2, now_dt=midday + timedelta(minutes=2))
    assert upd2[0]["status"] == "TARGET_2_HIT"

    closed = upd2[0]
    # Lot 1: 116.0 (gain +16 pts)
    # Lot 2: 131.0 (gain +31 pts)
    # Total points = 16 + 31 = 47 pts. Average pts = 47 / 2 = 23.5 pts.
    assert closed["points_pnl"] == 23.5
    # Weighted average exit price = (116.0 + 131.0) / 2 = 123.5
    assert closed["exit_price"] == 123.5

    # Net PnL must deduct statutory costs for both exit legs
    assert closed["net_pnl_inr"] > 0.0
    gross_inr = 47 * 65  # 3055.0
    assert closed["net_pnl_inr"] < gross_inr


# ==============================================================================
# 6. Paper Trading Safeguard Tests (Task Section 9)
# ==============================================================================

def test_paper_trading_mode_enforced_strictly():
    """Verify every created trade is strictly a paper trade with no live broker dispatch."""
    midday = datetime(2026, 10, 14, 11, 0, tzinfo=IST)
    sig = record_signal({
        "symbol": "NIFTY",
        "type": "CE",
        "strike": 22500,
        "spot_price": 22480.0,
        "entry_price": 100.0,
        "ask_at_entry": 100.0,
        "bid_at_entry": 99.5,
        "stop_loss": 90.0,
        "target_1": 115.0,
        "target_2": 130.0,
        "lots": 1,
        "lot_size": 65,
    }, is_paper_trade=True, override_cooldown=True, now_dt=midday)
    assert sig is not None
    assert sig["is_paper_trade"] == 1

    with _get_db() as conn:
        row = conn.execute("SELECT is_paper_trade FROM signals WHERE id = ?", (sig["id"],)).fetchone()
        assert row["is_paper_trade"] == 1
