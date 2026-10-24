"""
scripts/run_two_year_ingestion.py — Full 2-Year Historical Ingestion Runner
Discovers and ingests all 105 NIFTY expiries between 2024-10-24 and 2026-10-23
with rate limiting, manifest resumability, and validation.
"""

import sys
import os
import json
import time

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from app.backtesting.fno_downloader import FnoHistoricalDownloader


def main():
    downloader = FnoHistoricalDownloader(rate_limit_per_sec=5.0)
    print("Discovering all expiries between 2024-10-24 and 2026-10-23...")
    expiries = downloader.fetch_expiry_dates("2024-10-24", "2026-10-23")
    print(f"Total expiries discovered: {len(expiries)}")

    completed = 0
    failed = 0
    total = len(expiries)

    for idx, exp in enumerate(expiries, 1):
        status = downloader._manifest.get("expiries", {}).get(exp, {}).get("status")
        if status == "completed":
            completed += 1
            if idx % 10 == 0 or idx == total:
                print(f"[{idx}/{total}] Expiry {exp} already completed. Total completed: {completed}")
            continue

        print(f"[{idx}/{total}] Ingesting expiry {exp}...")
        success, report, _ = downloader.download_expiry(exp, strikes_around_atm=5)
        if success and report is not None:
            completed += 1
            print(f"[{idx}/{total}] Done {exp}: {report.total_rows} rows, {report.reconstructable_snapshots} snapshots.")
        else:
            failed += 1
            reasons = report.rejection_reasons if report else ["Download error"]
            print(f"[{idx}/{total}] FAILED {exp}: {reasons}")

    print("\n--- INGESTION SUMMARY ---")
    print(f"Total Discovered: {total}")
    print(f"Total Completed: {completed}")
    print(f"Total Failed: {failed}")


if __name__ == "__main__":
    main()
