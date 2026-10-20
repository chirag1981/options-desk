"""
scripts/run_strategy_diagnostic.py — CLI Runner for Autonomous Strategy Diagnostic & Optimization Agent
Executes trade failure detection, root cause diagnosis, counterfactual tick replay,
parameter optimization, and regression testing.

Usage:
  python scripts/run_strategy_diagnostic.py --once
  python scripts/run_strategy_diagnostic.py --loop --interval 300
  python scripts/run_strategy_diagnostic.py --auto-apply
"""

import os
import sys
import time
import argparse
import logging

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from app.services.strategy_diagnostic_agent import run_strategy_diagnostic_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("strategy_diagnostic_runner")


def format_diagnostic_report(report: dict) -> str:
    lines = []
    lines.append("=" * 80)
    lines.append(f"  OPTIONS STRATEGY DIAGNOSTIC & OPTIMIZATION REPORT ({report.get('timestamp')})")
    lines.append("=" * 80)
    lines.append(f"Overall Release / Engine Health: {report.get('status')}")
    lines.append(f"Total Failed / Closed Trades Inspected: {report.get('total_failed_trades_reviewed')}\n")

    lines.append("1. TRADE FAILURE DIAGNOSES:")
    lines.append("-" * 80)
    for d in report.get("diagnoses", []):
        sid = d.get("signal_id")
        sym = d.get("symbol")
        contract = d.get("contract")
        clf = d.get("classification")
        r_mult = d.get("r_multiple", 0.0)
        pnl = d.get("net_pnl_inr", 0.0)
        lines.append(f"• Trade #{sid} [{sym} {contract}] | Result: {clf} ({r_mult:+.2f}R | ₹{pnl:+,.2f})")
        for rc in d.get("root_causes", []):
            lines.append(f"    - {rc}")
    lines.append("")

    lines.append("2. COUNTERFACTUAL PARAMETER REPLAY ON TICKS:")
    lines.append("-" * 80)
    cf = report.get("counterfactual_replay", {})
    if cf.get("status") == "SUCCESS":
        lines.append(f"Best Performing Variant: {cf.get('best_variant')}")
        b_stats = cf.get("best_variant_stats", {})
        lines.append(f"Total R: {b_stats.get('total_r_multiple'):+.2f}R | Win Rate: {b_stats.get('win_rate_pct')}% | Delta vs Baseline: {b_stats.get('delta_r_vs_baseline'):+.2f}R\n")
        lines.append(f"{'Variant':<22} {'Total R':<12} {'Avg R':<10} {'Win %':<10} {'Delta R':<10}")
        lines.append("-" * 64)
        for v_name, stats in cf.get("comparison", {}).items():
            lines.append(f"{v_name:<22} {stats.get('total_r_multiple', 0.0):<+12.2f} {stats.get('avg_r_multiple', 0.0):<10.3f} {stats.get('win_rate_pct', 0.0):<10.1f} {stats.get('delta_r_vs_baseline', 0.0):<+10.2f}")
    else:
        lines.append(f"Replay Status: {cf.get('message', 'No trades evaluated')}")
    lines.append("")

    lines.append("3. OPTIMIZATION ACTIONS & RECOMMENDATIONS:")
    lines.append("-" * 80)
    recs = report.get("recommendations", [])
    if recs:
        for r in recs:
            lines.append(f"• Candidate: {r.get('target_param')} -> {r.get('new_value')} ({r.get('reason')})")
        for act in report.get("optimization_actions", []):
            lines.append(f"  Action Status: [{act.get('status')}] {act.get('message')}")
    else:
        lines.append("• No parameter adjustments warranted (Baseline rules remain statistically optimal).")
    lines.append("")

    lines.append("4. ENGINE HEALTH VERIFICATION:")
    lines.append("-" * 80)
    eh = report.get("engine_health_verification", {})
    lines.append(f"• analyze_option_desk(): {'PASS' if eh.get('passed') else 'FAIL'} | Decision: {eh.get('action')} | Score: {eh.get('setup_score')}")
    lines.append("=" * 80)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Autonomous Options Strategy Diagnostic & Optimization Runner")
    parser.add_argument("--once", action="store_true", default=True, help="Run a single diagnostic cycle and exit (default)")
    parser.add_argument("--loop", action="store_true", help="Run continuously in a loop")
    parser.add_argument("--interval", type=int, default=300, help="Interval between diagnostic cycles in seconds (default: 300)")
    parser.add_argument("--auto-apply", action="store_true", help="Automatically apply and verify parameter optimizations")
    args = parser.parse_args()

    if args.loop:
        log.info(f"Starting continuous Strategy Diagnostic loop (interval: {args.interval}s, auto_apply: {args.auto_apply})")
        while True:
            try:
                rep = run_strategy_diagnostic_cycle(auto_apply=args.auto_apply)
                print(format_diagnostic_report(rep))
            except Exception as e:
                log.error(f"Error during diagnostic cycle: {e}", exc_info=True)
            time.sleep(args.interval)
    else:
        rep = run_strategy_diagnostic_cycle(auto_apply=args.auto_apply)
        print(format_diagnostic_report(rep))


if __name__ == "__main__":
    main()
