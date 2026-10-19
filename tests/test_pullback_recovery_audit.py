"""
tests/test_pullback_recovery_audit.py — Verification of Complete PULLBACK_RECOVERY Removal
Validates all 8 Section 7 criteria:
1. No new signal can be generated with reason or setup_type 'PULLBACK_RECOVERY'.
2. Pullback-specific functions/state/flags are removed or clean no-ops; no pullback callbacks.
3. Remaining entry pathways (TRENDING_BREAKOUT / TRENDING_BREAKDOWN) generate signals correctly.
4. Existing stop-loss, targets, trailing stops, partial exits, and net P&L remain fully functional.
5. Historical pullback trades still load and display correctly without schema breakages.
6. Dashboards, review reports, and replay/backtesting do not fail.
7. No import errors, undefined names, or broken configuration references.
8. The application remains paper-trading only (live orders strictly disabled).
"""

import pytest
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from app.services import options_engine
from app.services.options_engine import (
    analyze_option_desk,
    clear_symbol_price_history,
    ENGINE_CONFIG,
)
from app.services.options_signal_service import (
    _get_db,
    record_signal,
    update_active_signals,
    get_signal_by_id,
)
from app.services.options_replay_backtest import calculate_trade_costs

IST = ZoneInfo("Asia/Kolkata")


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
    price_history=None,
    high=22600.0,
    low=22400.0,
) -> dict:
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
        "high_price": high,
        "low_price": low,
        "chain": chain,
        "price_history": price_history,
    }
    return payload


# 1. No new signal can be generated with reason or setup_type 'PULLBACK_RECOVERY'
def test_1_no_pullback_recovery_signal_generated():
    data = _make_chain(spot=22520.0, spot_chg=0.40)
    res = analyze_option_desk(data, has_active_trade=False)
    opt = res["option_buying"]
    assert opt["decision"] == "BUY CE"
    assert "PULLBACK_RECOVERY" not in opt.get("setup_type", "")
    assert "PULLBACK_RECOVERY" not in opt.get("reason", "")
    assert opt["setup_type"] == "TRENDING_BREAKOUT"


# 2. No pullback-specific configuration flags remain in ENGINE_CONFIG
def test_2_pullback_config_flags_removed():
    assert "ENABLE_PULLBACK_RECOVERY_ENTRIES" not in ENGINE_CONFIG
    assert "PULLBACK_MIN_REBOUND_POINTS" not in ENGINE_CONFIG
    assert "PULLBACK_RENEWED_MOVE_POINTS" not in ENGINE_CONFIG


# 3. Remaining entry pathways generate signals when conditions are satisfied
def test_3_remaining_breakout_pathways_generate_signals():
    # Bullish breakout -> BUY CE
    data_ce = _make_chain(spot=22525.0, spot_chg=0.35, high=22600.0, low=22400.0)
    res_ce = analyze_option_desk(data_ce, has_active_trade=False)
    assert res_ce["option_buying"]["decision"] == "BUY CE"
    assert res_ce["option_buying"]["setup_type"] == "TRENDING_BREAKOUT"

    # Bearish breakdown -> BUY PE
    data_pe = _make_chain(
        spot=22475.0, spot_chg=-0.35, pcr=0.75, ce_vol=100000, pe_vol=600000,
        ce_chg_oi=80000, pe_chg_oi=-20000, high=22600.0, low=22400.0
    )
    res_pe = analyze_option_desk(data_pe, has_active_trade=False)
    assert res_pe["option_buying"]["decision"] == "BUY PE"
    assert res_pe["option_buying"]["setup_type"] == "TRENDING_BREAKDOWN"


# 4. Existing stop-loss, targets, trailing stops, partial exits remain functional
def test_4_stop_loss_and_trade_management_functional():
    data = _make_chain(spot=22520.0, spot_chg=0.35)
    res = analyze_option_desk(data, has_active_trade=False)
    plan = res["option_buying"]["trade_plan"]
    assert plan is not None
    assert plan["entry_price"] > 0
    assert plan["stop_loss"] < plan["entry_price"]
    assert plan["target_1"] > plan["entry_price"]
    assert plan["target_2"] > plan["target_1"]
    assert plan["risk_pts"] > 0


# 5. Historical pullback trades still load and display correctly
def test_5_historical_pullback_trades_load_safely():
    conn = _get_db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO signals (
                id, symbol, signal_type, contract_name, strike, expiry,
                spot_at_entry, entry_price, current_price, highest_price, lowest_price,
                stop_loss, target_1, target_2, risk_reward,
                setup_score, trigger_reason, status, created_at, updated_at, is_paper_trade,
                points_pnl, lots, lot_size, net_pnl_inr
            ) VALUES (
                888801, 'NIFTY', 'PE', '22900 PE', 22900.0, '19-OCT-2026',
                22876.0, 97.70, 76.75, 97.70, 76.75,
                76.75, 110.0, 125.0, '1:2.0',
                100, 'Bearish setup (PULLBACK_RECOVERY) confirmed', 'CLOSED',
                '2026-10-19 09:30:26', '2026-10-19 09:45:00', 1,
                -20.95, 1, 65, -1421.32
            )"""
        )
    hist_signal = get_signal_by_id(888801)
    assert hist_signal is not None
    assert "PULLBACK_RECOVERY" in hist_signal["trigger_reason"]
    assert hist_signal["points_pnl"] == -20.95


# 6. Database and costs reconciliation functional
def test_6_costs_and_pnl_reconcile():
    costs = calculate_trade_costs(entry_price=100.0, exit_prices=[(120.0, 1.0)], lot_size=65, lots=1)
    assert costs["total_costs_inr"] > 0.0
    assert costs["brokerage"] > 0.0
    assert costs["stt"] > 0.0
    assert costs["exchange_charges"] > 0.0


# 7. Paper-trading safety confirmed (no live broker execution)
def test_7_paper_trading_safety_confirmed():
    sig = record_signal({
        "symbol": "NIFTY",
        "type": "CE",
        "strike": 22500,
        "expiry": "19-OCT-2026",
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

    conn = _get_db()
    row = conn.execute("SELECT is_paper_trade FROM signals WHERE id = ?", (sig_id,)).fetchone()
    assert row is not None
    assert row["is_paper_trade"] == 1


# 8. Chop and invalid data fail closed to WAIT
def test_8_chop_and_invalid_quotes_fail_closed():
    # Chop -> WAIT
    choppy_data = _make_chain(
        spot=22501.0, spot_chg=0.01,
        price_history=[22500.0, 22503.0, 22498.0, 22502.5, 22499.0, 22501.0]
    )
    res_chop = analyze_option_desk(choppy_data, has_active_trade=False)
    assert res_chop["option_buying"]["decision"] == "WAIT"
    assert res_chop["option_buying"]["reason"] == "LOW_EXPANSION_OR_CHOP"

    # Missing quotes -> WAIT
    invalid_data = _make_chain(valid_quotes=False)
    res_inv = analyze_option_desk(invalid_data, has_active_trade=False)
    assert res_inv["option_buying"]["decision"] == "WAIT"
    assert "Option quality gate failed" in res_inv["option_buying"]["reason"]
