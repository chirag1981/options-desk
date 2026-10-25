"""
scripts/credit_quality_experiment.py — One-Factor Credit Quality Experiment Runner
"""

import glob
import os
import json
import sys

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from scripts.run_strategy_experiments import ResearchBacktestEngine, ExperimentConfig
from app.backtesting.backtest_engine import STARTING_EQUITY

data_dir = os.path.join(WORKSPACE_ROOT, "instance", "historical_data")
files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
in_sample = files[:75]
out_sample = files[75:]

experiments = [
    ExperimentConfig(
        name="CQ-0: Control Baseline",
        description="Unaltered Control (Min Credit 10pts, Cost/Profit <= 30%)",
        min_credit_pts=10.0,
        max_cost_to_credit_pct=30.0,
    ),
    ExperimentConfig(
        name="CQ-1: Min Credit >= 23.0 pts",
        description="Eliminate low-margin tail (< 23pts credit)",
        min_credit_pts=23.0,
        max_cost_to_credit_pct=30.0,
    ),
    ExperimentConfig(
        name="CQ-2: Min Credit >= 25.0 pts",
        description="Standard 50% wing width credit rule (>= 25pts credit)",
        min_credit_pts=25.0,
        max_cost_to_credit_pct=30.0,
    ),
    ExperimentConfig(
        name="CQ-3: Cost/Profit <= 12.5%",
        description="Round-trip costs must not exceed 12.5% of gross max profit",
        min_credit_pts=10.0,
        max_cost_to_credit_pct=12.5,
    ),
    ExperimentConfig(
        name="CQ-4: Min Credit >= 27.0 pts",
        description="Upper median credit filter (>= 27pts credit)",
        min_credit_pts=27.0,
        max_cost_to_credit_pct=30.0,
    ),
]

results = []

for exp in experiments:
    row = {"name": exp.name, "desc": exp.description, "discovery": {}, "holdout": {}}
    for split_name, split_files in [("discovery", in_sample), ("holdout", out_sample)]:
        engine = ResearchBacktestEngine(experiment=exp, data_dir=data_dir, starting_equity=STARTING_EQUITY)
        res = engine.run_simulation(parquet_files=split_files)
        for sc in ["BASE", "CONSERVATIVE"]:
            m = res[sc]["IRON_CONDOR"]
            row[split_name][sc] = {
                "trades": m.total_trades,
                "wins": m.winning_trades,
                "losses": m.losing_trades,
                "win_rate": m.win_rate_pct,
                "gross_pnl": m.gross_pnl,
                "costs": m.total_costs,
                "net_pnl": m.net_pnl,
                "pf": m.profit_factor,
                "exp": m.expectancy_inr,
                "max_dd": m.max_drawdown_inr,
                "streak": m.max_consecutive_losses,
            }
    results.append(row)

print("\n" + "=" * 125)
print(f"{'Experiment':<32} | {'Split':<10} | {'Scenario':<12} | {'Trades':<6} | {'Win%':<6} | {'Gross PnL':<11} | {'Costs':<9} | {'Net PnL':<11} | {'PF':<5} | {'Expectancy':<10} | {'Max DD':<10}")
print("=" * 125)

for r in results:
    exp_name = r["name"]
    for split in ["discovery", "holdout"]:
        for sc in ["BASE", "CONSERVATIVE"]:
            m = r[split][sc]
            print(f"{exp_name:<32} | {split:<10} | {sc:<12} | {m['trades']:<6} | {m['win_rate']:<5.1f}% | {m['gross_pnl']:<11.2f} | {m['costs']:<9.2f} | {m['net_pnl']:<11.2f} | {m['pf']:<5.2f} | {m['exp']:<10.2f} | {m['max_dd']:<10.2f}")
    print("-" * 125)

out_file = os.path.join(data_dir, "credit_quality_experiment_results.json")
with open(out_file, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print(f"\nSaved results to {out_file}")
