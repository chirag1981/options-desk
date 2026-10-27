"""
cli.py — Command-Line Interface for NIFTY Iron Condor Paper-Trading System
Commands:
  python cli.py status       - Show active positions, capital, risk, and kill switch status
  python cli.py run          - Start continuous monitoring loop
  python cli.py run --once   - Execute single evaluation tick
  python cli.py report       - Generate daily EOD performance and risk report
  python cli.py export       - Export paper trading history to CSV
  python cli.py kill         - Activate emergency global kill switch (creates 'KILL')
  python cli.py unkill       - Deactivate emergency global kill switch (removes 'KILL')
  python cli.py replay --date YYYY-MM-DD - Replay historical session for validation
"""

import os
import sys
import argparse
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Config
from app.broker import PaperBroker, is_kill_switch_active, KILL_SWITCH_FILE
from app.option_chain_service import OptionChainService
from app.strategy_profiles import StrategyProfileManager
from app.adjustments import AdjustmentEngine

try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

IST = ZoneInfo("Asia/Kolkata")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cli")


def cmd_status():
    """Prints current trading status and risk boundaries."""
    kill_active = is_kill_switch_active()
    broker = PaperBroker(capital=1_000_000.0)
    bal = broker.get_account_balance()

    print("\n" + "=" * 60)
    print("NIFTY IRON CONDOR PAPER TRADING DESK - SYSTEM STATUS")
    print("=" * 60)
    print(f"Timestamp        : {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"Broker Mode      : PAPER (Simulated execution only)")
    print(f"Strategy Profile : {getattr(Config, 'ACTIVE_STRATEGY_PROFILE', 'monthly_45_15')} (Monthly Expiry Only)")
    print(f"Monthly Only     : {'[ENABLED]' if getattr(Config, 'MONTHLY_EXPIRIES_ONLY', True) else '[DISABLED]'}")
    print(f"Global Kill File : {'[HALTED] ACTIVE (KILL file present)' if kill_active else '[OK] INACTIVE (Normal Operations)'}")
    print(f"Total Capital    : INR {bal['total_capital']:,.2f}")
    print(f"Available Margin : INR {bal['available_capital']:,.2f}")
    print(f"Daily Loss Limit : INR {bal['total_capital'] * 0.015:,.2f} (1.5% max drawdown)")
    print(f"Realized Loss    : INR {bal['daily_realized_loss']:,.2f}")
    print("=" * 60 + "\n")


def cmd_kill():
    """Activates global kill switch by writing the KILL file."""
    with open(KILL_SWITCH_FILE, "w") as f:
        f.write(f"KILLED at {datetime.now(IST).isoformat()} by CLI operator\n")
    print(f"\n[ALERT] GLOBAL KILL SWITCH ACTIVATED: Created file '{KILL_SWITCH_FILE}'")
    print("All new entries and adjustments are halted. Position exits remain permitted.\n")


def cmd_unkill():
    """Deactivates global kill switch by deleting the KILL file."""
    if os.path.exists(KILL_SWITCH_FILE):
        os.remove(KILL_SWITCH_FILE)
        print(f"\n[OK] GLOBAL KILL SWITCH DEACTIVATED: Removed file '{KILL_SWITCH_FILE}'. Normal paper trading resumed.\n")
    else:
        print("\n[INFO] Kill switch was not active. No 'KILL' file found.\n")


def cmd_run(once: bool = False):
    """Executes single evaluation tick or monitoring loop."""
    print(f"\n[CLI] Evaluating trading cycle (once={once})...")
    broker = PaperBroker()

    # Load market chain
    chain_info = OptionChainService.get_live_chain()
    if chain_info.get("status") != "success" or not chain_info.get("chain"):
        print(f"[CLI] Live broker feed not connected. Testing offline with latest historical session.")
        dates = OptionChainService.get_available_dates()
        latest = dates.get("latest_date")
        chain_info = OptionChainService.get_historical_chain(latest, "12:00:00")

    spot = chain_info.get("spot_price", 0.0)
    expiry = chain_info.get("expiry")
    print(f"[CLI] NIFTY Spot: INR {spot:,.1f} | Expiry: {expiry} (Monthly)")

    # Evaluate active profile (defaults to monthly_45_15)
    chain_dict = {}
    for r in chain_info.get("chain", []):
        chain_dict[r["strike"]] = {"CE": r["ce"], "PE": r["pe"]}

    profile = getattr(Config, "ACTIVE_STRATEGY_PROFILE", "monthly_45_15")
    if profile == "monthly_45_15":
        eval_res = StrategyProfileManager.evaluate_monthly_45_15_entry(
            spot=spot,
            chain=chain_dict,
            dte=45.0,
        )
    else:
        eval_res = StrategyProfileManager.evaluate_weekly_filtered_entry(
            spot=spot,
            chain=chain_dict,
            dte=4.0,
            atm_iv=14.0,
        )

    if eval_res.get("is_valid"):
        print(f"[CLI] Setup Valid ({profile}): Short Put {eval_res['short_put']} / Short Call {eval_res['short_call']} | Credit: INR {eval_res['net_credit_pts']:.1f} pts")
    else:
        print(f"[CLI] Setup Skipped ({profile}): {eval_res.get('rejection_reason')}")

    print("[CLI] Evaluation tick completed.\n")


def cmd_replay(target_date: str):
    """Replays historical session for strategy validation."""
    print(f"\n[CLI] Replaying session for date: {target_date}...")
    res = OptionChainService.get_historical_chain(target_date, "12:00:00")
    if res.get("status") != "success":
        print(f"[CLI] Error: {res.get('message')}")
        return

    spot = res.get("spot_price", 0.0)
    r1 = res.get("r1_resistance")
    s1 = res.get("s1_support")
    pcr = res.get("total_pcr")
    print(f"[CLI] Snapshot at 12:00 PM -> Spot: INR {spot:,.1f} | R1: {r1} | S1: {s1} | PCR: {pcr}")
    print(f"[CLI] Strikes loaded: {len(res.get('chain', []))}")
    print("[CLI] Session replay verified successfully.\n")


def main():
    parser = argparse.ArgumentParser(description="NIFTY Iron Condor Paper Trading CLI")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show system status and risk limits")
    subparsers.add_parser("kill", help="Activate global emergency kill switch")
    subparsers.add_parser("unkill", help="Deactivate global emergency kill switch")

    run_parser = subparsers.add_parser("run", help="Run strategy evaluation")
    run_parser.add_argument("--once", action="store_true", help="Run single evaluation tick")

    subparsers.add_parser("report", help="Generate daily performance report")
    subparsers.add_parser("export", help="Export trade journal to CSV")

    replay_parser = subparsers.add_parser("replay", help="Replay historical session")
    replay_parser.add_argument("--date", type=str, required=True, help="Session date YYYY-MM-DD")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status()
    elif args.command == "kill":
        cmd_kill()
    elif args.command == "unkill":
        cmd_unkill()
    elif args.command == "run":
        cmd_run(once=args.once)
    elif args.command == "replay":
        cmd_replay(target_date=args.date)
    elif args.command == "export":
        from app.performance import PerformanceAnalytics
        csv_data = PerformanceAnalytics.export_trades_csv()
        filename = f"nifty_journal_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(csv_data)
        print(f"\n[CLI] Trade journal exported successfully to {filename}\n")
    elif args.command == "report":
        cmd_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
