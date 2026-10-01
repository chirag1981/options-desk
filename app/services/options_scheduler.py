"""
app/services/options_scheduler.py — Background Data Refresher (Every 3 Minutes)
Automatically refreshes Option Chains for primary indices in background threads
to keep analytical desk data warm and up to date without blocking user requests.
"""

import time
import threading
import logging
from app.services.fyers_options_service import fetch_option_chain_data, INDEX_CONFIGS
from app.services.options_engine import analyze_option_desk

log = logging.getLogger("options_scheduler")
_scheduler_thread = None
_stop_scheduler = threading.Event()


def _refresh_worker():
    """Periodic worker that polls primary indices every 180 seconds (3 mins)."""
    log.info("Options Desk 3-minute background updater started.")
    while not _stop_scheduler.is_set():
        try:
            for sym in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]:
                if _stop_scheduler.is_set():
                    break
                try:
                    raw = fetch_option_chain_data(symbol=sym, force_refresh=True)
                    analyze_option_desk(raw)
                except Exception as e:
                    log.warning(f"Background refresh for {sym} failed: {e}")
                time.sleep(2) # Slight stagger between symbols
        except Exception as e:
            log.error(f"Error in options background worker: {e}")

        # Sleep in short increments to allow graceful shutdown
        for _ in range(180):
            if _stop_scheduler.is_set():
                break
            time.sleep(1)


def start_options_scheduler(app=None):
    """Starts background thread if not already running."""
    global _scheduler_thread
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _stop_scheduler.clear()
        _scheduler_thread = threading.Thread(target=_refresh_worker, daemon=True, name="OptionsDeskUpdater")
        _scheduler_thread.start()
