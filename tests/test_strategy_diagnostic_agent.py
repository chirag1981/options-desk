"""
tests/test_strategy_diagnostic_agent.py — Test Suite for Strategy Diagnostic & Optimization Agent
Validates:
1. Trade Failure Detection
2. 4-Pillar Root Cause Diagnosis
3. Counterfactual Replay Engine
4. Safe Optimization & Rollback
5. Live Engine Health Verification
"""

import pytest
from app.services.options_signal_service import record_signal, _get_db
from app.services.strategy_diagnostic_agent import (
    detect_failed_trades,
    diagnose_trade_root_cause,
    replay_counterfactual_parameter_grid,
    apply_strategy_optimization,
    verify_engine_health,
    run_strategy_diagnostic_cycle,
)
from app.services.options_engine import ENGINE_CONFIG


@pytest.fixture
def sample_failed_signal():
    """Creates a sample failed signal and ticks in the isolated test DB."""
    sig_payload = {
        "symbol": "NIFTY",
        "signal_type": "PE",
        "contract_name": "23000 PE",
        "strike": 23000,
        "expiry": "2026-10-22",
        "spot_at_entry": 22985.0,
        "entry_price": 100.0,
        "bid_at_entry": 99.8,
        "ask_at_entry": 100.2,
        "stop_loss": 92.0,
        "target_1": 115.0,
        "target_2": 125.0,
        "risk_reward": "1:1.8",
        "setup_score": 75,
        "pcr": 0.90,
        "bias": "BEARISH",
        "atm_iv": 14.5,
        "dte": 3.0,
        "moneyness": "ATM",
        "pillar_flags": {"direction": True, "level": True, "regime": True},
        "raw_values": {},
        "data_quality": {"valid": True},
        "reason": "Test setup",
        "spread_paid": 0.25,
    }
    sig_res = record_signal(sig_payload, is_paper_trade=True)
    sig_id = sig_res["id"] if isinstance(sig_res, dict) else sig_res

    with _get_db() as conn:
        # Mark as SL_HIT
        conn.execute("""
            UPDATE signals
            SET status = 'SL_HIT', exit_price = 91.5, exit_time = '2026-10-19 09:35:00',
                points_pnl = -8.5, net_pnl_inr = -500.0, exit_reason = 'STOP_LOSS_HIT',
                duration_mins = 5.0
            WHERE id = ?
        """, (sig_id,))

        # Insert sample ticks
        conn.execute("""
            INSERT INTO trade_ticks (signal_id, timestamp, ltp, bid, ask, spot, created_at)
            VALUES (?, '2026-10-19 09:30:00', 100.0, 99.8, 100.2, 22985.0, '2026-10-19 09:30:00')
        """, (sig_id,))
        conn.execute("""
            INSERT INTO trade_ticks (signal_id, timestamp, ltp, bid, ask, spot, created_at)
            VALUES (?, '2026-10-19 09:35:00', 91.5, 91.3, 91.7, 22995.0, '2026-10-19 09:35:00')
        """, (sig_id,))

    return sig_id


def test_detect_failed_trades(sample_failed_signal):
    trades = detect_failed_trades(limit=10)
    assert isinstance(trades, list)
    assert len(trades) >= 1
    t = trades[0]
    assert t["id"] == sample_failed_signal
    assert t["symbol"] == "NIFTY"
    assert t["status"] == "SL_HIT"


def test_diagnose_trade_root_cause(sample_failed_signal):
    diag = diagnose_trade_root_cause(sample_failed_signal)
    assert diag["signal_id"] == sample_failed_signal
    assert "pillar_findings" in diag
    assert "root_causes" in diag
    assert "pivot_and_strike_defense" in diag["pillar_findings"]
    assert "trending_oi_and_bias" in diag["pillar_findings"]
    assert "timing_iv_and_spread" in diag["pillar_findings"]
    assert "stop_loss_buffers" in diag["pillar_findings"]


def test_replay_counterfactual_parameter_grid(sample_failed_signal):
    res = replay_counterfactual_parameter_grid()
    assert res["status"] in ("SUCCESS", "NO_TRADES")
    if res["status"] == "SUCCESS":
        assert "comparison" in res
        assert "BASELINE_15_PCT" in res["comparison"]
        assert "best_variant" in res


def test_apply_strategy_optimization_dry_run():
    rec = {
        "target_param": "NIFTY_MAX_OPTION_RISK_POINTS",
        "new_value": 10.0,
        "reason": "Test optimization",
    }
    res = apply_strategy_optimization(rec, dry_run=True)
    assert res["status"] == "DRY_RUN"
    assert res["target_param"] == "NIFTY_MAX_OPTION_RISK_POINTS"
    assert res["new_value"] == 10.0


def test_verify_engine_health():
    health = verify_engine_health()
    assert health["passed"] is True
    assert health["action"] in ("CE", "PE", "BUY CE", "BUY PE", "WAIT")
    assert health["setup_score"] is not None


def test_run_strategy_diagnostic_cycle(sample_failed_signal):
    report = run_strategy_diagnostic_cycle(auto_apply=False)
    assert report["status"] == "PASS"
    assert "diagnoses" in report
    assert "counterfactual_replay" in report
    assert "engine_health_verification" in report
    assert report["total_failed_trades_reviewed"] >= 1
