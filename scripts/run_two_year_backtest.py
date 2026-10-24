"""
scripts/run_two_year_backtest.py — Full 2-Year Historical Simulation Runner
Executes the high-fidelity backtest over all 105 validated Parquet datasets for:
- BULL_PUT
- BEAR_CALL
- IRON_CONDOR
Across OPTIMISTIC, BASE, and CONSERVATIVE fill scenarios.
"""

import sys
import os
import json
import glob
import time
from dataclasses import asdict

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from app.backtesting.backtest_engine import HistoricalBacktestEngine, STARTING_EQUITY


def main():
    start_time = time.time()
    data_dir = os.path.join(WORKSPACE_ROOT, "instance", "historical_data")
    parquet_files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
    print(f"Loaded {len(parquet_files)} validated Parquet files for backtest simulation.")

    engine = HistoricalBacktestEngine(
        data_dir=data_dir,
        starting_equity=STARTING_EQUITY,
        target_profit_pct=0.50,
        stop_loss_credit_mult=1.0,
    )

    print("Running simulation for OPTIMISTIC, BASE, and CONSERVATIVE scenarios...")
    results = engine.run_simulation(parquet_files=parquet_files)

    elapsed = time.time() - start_time
    print(f"Simulation completed in {elapsed:.2f} seconds.")

    # Format structured summary
    summary = {}
    for scenario, strat_dict in results.items():
        summary[scenario] = {}
        for strat_name, metrics in strat_dict.items():
            summary[scenario][strat_name] = {
                "total_trades": metrics.total_trades,
                "winning_trades": metrics.winning_trades,
                "losing_trades": metrics.losing_trades,
                "win_rate_pct": metrics.win_rate_pct,
                "gross_pnl": round(metrics.gross_pnl, 2),
                "total_brokerage": round(metrics.total_brokerage, 2),
                "total_taxes": round(metrics.total_taxes, 2),
                "total_costs": round(metrics.total_costs, 2),
                "total_slippage": round(metrics.total_slippage, 2),
                "net_pnl": round(metrics.net_pnl, 2),
                "roi_pct": metrics.roi_pct,
                "profit_factor": metrics.profit_factor,
                "expectancy_inr": metrics.expectancy_inr,
                "max_drawdown_inr": metrics.max_drawdown_inr,
                "max_drawdown_pct": metrics.max_drawdown_pct,
                "largest_win_inr": metrics.largest_win_inr,
                "largest_loss_inr": metrics.largest_loss_inr,
                "max_consecutive_losses": metrics.max_consecutive_losses,
                "monthly_pnl": {k: round(v, 2) for k, v in sorted(metrics.monthly_pnl.items())},
                "pnl_by_regime": {k: round(v, 2) for k, v in sorted(metrics.pnl_by_regime.items())},
            }

    # Save summary report
    out_file = os.path.join(data_dir, "backtest_results_summary.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Saved full results to {out_file}")

    # Print summary table to console
    print("\n============================== BASE SCENARIO PERFORMANCE ==============================")
    base_res = summary.get("BASE", {})
    for strat, m in base_res.items():
        print(f"\n[{strat}]")
        print(f"  Trades: {m['total_trades']} | Win Rate: {m['win_rate_pct']}% (W: {m['winning_trades']}, L: {m['losing_trades']})")
        print(f"  Gross P&L: INR {m['gross_pnl']:,.2f} | Costs: INR {m['total_costs']:,.2f} | Slippage: INR {m['total_slippage']:,.2f}")
        print(f"  Net P&L: INR {m['net_pnl']:,.2f} | ROI: {m['roi_pct']}% | Expectancy: INR {m['expectancy_inr']:,.2f}")
        print(f"  Profit Factor: {m['profit_factor']} | Max DD: INR {m['max_drawdown_inr']:,.2f} ({m['max_drawdown_pct']}%) | Max Streak: {m['max_consecutive_losses']}")


if __name__ == "__main__":
    main()
