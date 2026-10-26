"""
scripts/reconcile_runs.py — Reconciled Baseline Simulation & Ledger Verification

Generates fully reconciled, verifiable baseline metrics across:
- Discovery Period (In-Sample: 75 expiry sessions, Oct 2024 to Mar 2026)
- Holdout Period (Out-of-Sample: 30 expiry sessions, Mar 2026 to Oct 2026)
- Full Period (105 expiry sessions)

Verifies strict mathematical additivity (Discovery + Holdout = Full Period)
and outputs structured JSON to instance/historical_data/reconciled_baseline_report.json.
"""

import os
import sys
import glob
import json
from dataclasses import asdict
from typing import Dict, Any, List

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from app.backtesting.backtest_engine import HistoricalBacktestEngine, STARTING_EQUITY
from app.config import Config

def run_reconciliation():
    data_dir = os.path.join(WORKSPACE_ROOT, "instance", "historical_data")
    all_files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
    is_files = all_files[:75]
    oos_files = all_files[75:]

    engine = HistoricalBacktestEngine(
        data_dir=data_dir,
        starting_equity=STARTING_EQUITY,
        target_profit_pct=0.70,
        stop_loss_credit_mult=0.50,
    )

    scenarios = ["OPTIMISTIC", "BASE", "CONSERVATIVE"]
    report = {
        "metadata": {
            "total_expiry_sessions": len(all_files),
            "discovery_sessions": len(is_files),
            "holdout_sessions": len(oos_files),
            "discovery_range": f"{os.path.basename(is_files[0])} to {os.path.basename(is_files[-1])}",
            "holdout_range": f"{os.path.basename(oos_files[0])} to {os.path.basename(oos_files[-1])}",
            "target_profit_pct": 0.70,
            "stop_loss_credit_mult": 0.50,
            "max_risk_per_trade_inr": Config.get_max_risk_per_trade_inr(),
            "max_combined_portfolio_risk_inr": Config.get_max_combined_portfolio_risk_inr(),
            "max_daily_loss_inr": Config.get_max_daily_loss_inr(),
            "consecutive_loss_cooldown_mins": Config.CONSECUTIVE_LOSS_COOLDOWN_MINS,
        },
        "discovery_in_sample": {},
        "holdout_out_of_sample": {},
        "full_period": {},
    }

    print("\n============================== RECONCILING HISTORICAL BASELINE ==============================")
    for sc in scenarios:
        print(f"\n[Scenario: {sc}]")
        res_is = engine._run_single_scenario(is_files, sc)
        res_oos = engine._run_single_scenario(oos_files, sc)
        res_full = engine._run_single_scenario(all_files, sc)

        m_is = res_is["COMBINED"]
        m_oos = res_oos["COMBINED"]
        m_full = res_full["COMBINED"]

        report["discovery_in_sample"][sc] = {
            s: _format_metrics(res_is[s]) for s in ["IRON_CONDOR", "COMBINED"]
        }
        report["holdout_out_of_sample"][sc] = {
            s: _format_metrics(res_oos[s]) for s in ["IRON_CONDOR", "COMBINED"]
        }
        report["full_period"][sc] = {
            s: _format_metrics(res_full[s]) for s in ["IRON_CONDOR", "COMBINED"]
        }

        print(f"  Discovery (75): Trades={m_is.total_trades} (W:{m_is.winning_trades}, L:{m_is.losing_trades}), Net PnL=INR {m_is.net_pnl:,.2f}, PF={m_is.profit_factor}")
        print(f"  Holdout   (30): Trades={m_oos.total_trades} (W:{m_oos.winning_trades}, L:{m_oos.losing_trades}), Net PnL=INR {m_oos.net_pnl:,.2f}, PF={m_oos.profit_factor}")
        print(f"  Full     (105): Trades={m_full.total_trades} (W:{m_full.winning_trades}, L:{m_full.losing_trades}), Net PnL=INR {m_full.net_pnl:,.2f}, PF={m_full.profit_factor}")
        print(f"  Additivity Check: {m_is.total_trades} + {m_oos.total_trades} = {m_is.total_trades + m_oos.total_trades} (Full: {m_full.total_trades}) -> {'PERFECT' if m_is.total_trades + m_oos.total_trades == m_full.total_trades else 'MISMATCH'}")

    out_path = os.path.join(data_dir, "reconciled_baseline_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved reconciled baseline report to: {out_path}")
    return report

def _format_metrics(m) -> Dict[str, Any]:
    return {
        "total_trades": m.total_trades,
        "winning_trades": m.winning_trades,
        "losing_trades": m.losing_trades,
        "win_rate_pct": m.win_rate_pct,
        "gross_pnl": m.gross_pnl,
        "total_brokerage": m.total_brokerage,
        "total_taxes": m.total_taxes,
        "total_costs": m.total_costs,
        "total_slippage": m.total_slippage,
        "net_pnl": m.net_pnl,
        "roi_pct": m.roi_pct,
        "profit_factor": m.profit_factor,
        "expectancy_inr": m.expectancy_inr,
        "max_drawdown_inr": m.max_drawdown_inr,
        "max_drawdown_pct": m.max_drawdown_pct,
        "largest_win_inr": m.largest_win_inr,
        "largest_loss_inr": m.largest_loss_inr,
        "max_consecutive_losses": m.max_consecutive_losses,
        "monthly_pnl": m.monthly_pnl,
        "pnl_by_regime": m.pnl_by_regime,
    }

if __name__ == "__main__":
    run_reconciliation()
