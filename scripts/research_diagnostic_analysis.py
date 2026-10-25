"""
scripts/research_diagnostic_analysis.py — Phase 2 Strategy Diagnostic Analysis

Analyzes the full 105-session baseline historical replay to identify:
- Performance by strategy, regime, bias, entry time, cushion, credit ratio, PCR, exit reason, and holding time.
- Impact of transaction costs on wins vs losses.
- Root-cause drivers of negative expectancy.
- Specific behavior of Iron Condor entries under CHOPPY_OR_INSUFFICIENT.
"""

import os
import sys
import json
import glob
import time
from collections import defaultdict
from dataclasses import asdict
import pandas as pd
import numpy as np

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from app.backtesting.backtest_engine import HistoricalBacktestEngine, STARTING_EQUITY
from app.config import Config


def summarize_trades(trade_list, starting_equity=STARTING_EQUITY):
    """Calculates comprehensive performance metrics for a subset of trades."""
    n = len(trade_list)
    if n == 0:
        return {
            "count": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
            "gross_pnl": 0.0, "costs": 0.0, "net_pnl": 0.0,
            "profit_factor": 0.0, "expectancy": 0.0, "max_dd": 0.0, "avg_holding_mins": 0.0
        }

    wins = [t for t in trade_list if t.net_pnl > 0]
    losses = [t for t in trade_list if t.net_pnl < 0]
    win_cnt = len(wins)
    loss_cnt = len(losses)
    win_rate = round((win_cnt / n) * 100.0, 2)

    gross_pnl = sum(t.gross_pnl for t in trade_list)
    total_costs = sum(t.total_costs for t in trade_list)
    net_pnl = sum(t.net_pnl for t in trade_list)

    gains = sum(t.net_pnl for t in wins)
    loss_sum = sum(abs(t.net_pnl) for t in losses)
    pf = round(gains / loss_sum, 2) if loss_sum > 0 else 99.99

    avg_win = (gains / win_cnt) if win_cnt > 0 else 0.0
    avg_loss = (loss_sum / loss_cnt) if loss_cnt > 0 else 0.0
    p_win = win_cnt / n
    p_loss = loss_cnt / n
    expectancy = round((p_win * avg_win) - (p_loss * avg_loss), 2)

    # Chronological Drawdown
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trade_list:
        cum += t.net_pnl
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    avg_hold = round(sum(t.holding_time_mins for t in trade_list) / n, 1)

    return {
        "count": n,
        "wins": win_cnt,
        "losses": loss_cnt,
        "win_rate_pct": win_rate,
        "gross_pnl": round(gross_pnl, 2),
        "costs": round(total_costs, 2),
        "net_pnl": round(net_pnl, 2),
        "profit_factor": pf,
        "expectancy": expectancy,
        "max_dd": round(max_dd, 2),
        "avg_holding_mins": avg_hold,
    }


def main():
    start_time = time.time()
    data_dir = os.path.join(WORKSPACE_ROOT, "instance", "historical_data")
    parquet_files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
    print(f"Loaded {len(parquet_files)} parquet files. Running baseline BASE replay...")

    engine = HistoricalBacktestEngine(
        data_dir=data_dir,
        starting_equity=STARTING_EQUITY,
        target_profit_pct=0.70,
        stop_loss_credit_mult=0.50,
    )

    scenario_res = engine.run_simulation(parquet_files=parquet_files, scenarios=["BASE"])
    base_trades = scenario_res["BASE"]["COMBINED"].trades

    print(f"Replay completed in {time.time() - start_time:.2f}s. Total trades: {len(base_trades)}")

    # Grouping breakdowns
    by_strategy = defaultdict(list)
    by_regime = defaultdict(list)
    by_bias = defaultdict(list)
    by_exit_reason = defaultdict(list)
    by_time_of_day = defaultdict(list)
    by_cushion = defaultdict(list)
    by_credit_ratio = defaultdict(list)
    by_pcr = defaultdict(list)
    by_holding_time = defaultdict(list)

    for t in base_trades:
        by_strategy[t.strategy_id].append(t)
        by_regime[t.market_regime_entry].append(t)
        by_bias[t.market_bias_entry].append(t)
        by_exit_reason[t.exit_reason].append(t)

        # Time of day
        hour = int(t.entry_time[11:13])
        minute = int(t.entry_time[14:16])
        time_mins = hour * 60 + minute
        if time_mins < 11 * 60:
            tod = "1. Morning (09:20 - 11:00)"
        elif time_mins < 13 * 60:
            tod = "2. Midday (11:00 - 13:00)"
        else:
            tod = "3. Afternoon (13:00 - 14:45)"
        by_time_of_day[tod].append(t)

        # Cushion
        cush = t.spot_cushion_pts
        if cush < 30.0:
            cush_grp = "1. Tight (< 30 pts)"
        elif cush <= 50.0:
            cush_grp = "2. Moderate (30 - 50 pts)"
        else:
            cush_grp = "3. Wide (> 50 pts)"
        by_cushion[cush_grp].append(t)

        # Credit ratio
        cr = t.credit_to_width_pct
        if cr < 25.0:
            cr_grp = "1. Low (< 25%)"
        elif cr <= 35.0:
            cr_grp = "2. Balanced (25% - 35%)"
        else:
            cr_grp = "3. High (> 35%)"
        by_credit_ratio[cr_grp].append(t)

        # PCR
        pcr = t.pcr_oi_entry
        if pcr < 0.90:
            pcr_grp = "1. Low Bearish (< 0.90)"
        elif pcr <= 1.10:
            pcr_grp = "2. Neutral (0.90 - 1.10)"
        else:
            pcr_grp = "3. High Bullish (> 1.10)"
        by_pcr[pcr_grp].append(t)

        # Holding time
        ht = t.holding_time_mins
        if ht < 30:
            ht_grp = "1. < 30 mins"
        elif ht < 60:
            ht_grp = "2. 30 - 60 mins"
        elif ht < 120:
            ht_grp = "3. 60 - 120 mins"
        else:
            ht_grp = "4. > 120 mins"
        by_holding_time[ht_grp].append(t)

    # Print Report
    def print_section(title, grp_dict):
        print(f"\n==================== {title.upper()} ====================")
        print(f"{'Category':<32} | {'Trades':<6} | {'Win%':<6} | {'Gross PnL':<11} | {'Costs':<9} | {'Net PnL':<11} | {'PF':<5} | {'Expectancy':<10} | {'Max DD':<10}")
        print("-" * 115)
        for cat in sorted(grp_dict.keys()):
            m = summarize_trades(grp_dict[cat])
            print(f"{cat:<32} | {m['count']:<6} | {m['win_rate_pct']:<5.1f}% | {m['gross_pnl']:<11.2f} | {m['costs']:<9.2f} | {m['net_pnl']:<11.2f} | {m['profit_factor']:<5.2f} | {m['expectancy']:<10.2f} | {m['max_dd']:<10.2f}")

    print_section("Breakdown by Strategy", by_strategy)
    print_section("Breakdown by Market Regime", by_regime)
    print_section("Breakdown by Market Bias", by_bias)
    print_section("Breakdown by Exit Reason", by_exit_reason)
    print_section("Breakdown by Time of Day", by_time_of_day)
    print_section("Breakdown by Spot-to-Short Strike Cushion", by_cushion)
    print_section("Breakdown by Credit / Width Ratio", by_credit_ratio)
    print_section("Breakdown by PCR at Entry", by_pcr)
    print_section("Breakdown by Holding Duration", by_holding_time)

    # Cost Drag Analysis
    total_gross = sum(t.gross_pnl for t in base_trades)
    total_costs = sum(t.total_costs for t in base_trades)
    total_net = sum(t.net_pnl for t in base_trades)
    winning_gross = sum(t.gross_pnl for t in base_trades if t.net_pnl > 0)
    losing_gross = sum(abs(t.gross_pnl) for t in base_trades if t.net_pnl < 0)

    print("\n==================== TRANSACTION COST DRAG ====================")
    print(f"Total Gross P&L:        INR {total_gross:,.2f}")
    print(f"Total Transaction Costs: INR {total_costs:,.2f} ({abs(total_costs/total_gross)*100.0 if total_gross != 0 else 0:.1f}% of gross)")
    print(f"Total Net P&L:          INR {total_net:,.2f}")
    print(f"Gross Gains (Wins):     INR {winning_gross:,.2f}")
    print(f"Gross Losses:           INR -{losing_gross:,.2f}")
    print(f"Costs as % of Gross Win: {total_costs / winning_gross * 100.0 if winning_gross > 0 else 0:.1f}%")

    # Deep dive: Iron Condors in CHOPPY_OR_INSUFFICIENT vs BALANCED_RANGE
    print("\n==================== IRON CONDOR REGIME DEEP DIVE ====================")
    ic_trades = by_strategy["IRON_CONDOR"]
    for t in ic_trades:
        print(f"Trade {t.trade_id} [{t.entry_time}]: Regime={t.market_regime_entry}, Bias={t.market_bias_entry}, Net Credit={t.net_credit_pts}, Exit={t.exit_reason}, Net PnL=INR {t.net_pnl:,.2f}")

    # Save diagnostic breakdown to JSON
    report_data = {
        "strategy": {k: summarize_trades(v) for k, v in by_strategy.items()},
        "regime": {k: summarize_trades(v) for k, v in by_regime.items()},
        "bias": {k: summarize_trades(v) for k, v in by_bias.items()},
        "exit_reason": {k: summarize_trades(v) for k, v in by_exit_reason.items()},
        "time_of_day": {k: summarize_trades(v) for k, v in by_time_of_day.items()},
        "cushion": {k: summarize_trades(v) for k, v in by_cushion.items()},
        "credit_ratio": {k: summarize_trades(v) for k, v in by_credit_ratio.items()},
        "pcr": {k: summarize_trades(v) for k, v in by_pcr.items()},
        "holding_time": {k: summarize_trades(v) for k, v in by_holding_time.items()},
    }

    out_file = os.path.join(data_dir, "diagnostic_breakdown_report.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    print(f"\nSaved structured diagnostic report to {out_file}")


if __name__ == "__main__":
    main()
