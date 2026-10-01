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
                    analysis = analyze_option_desk(raw)
                    
                    # Auto-record high-conviction signal if generated
                    opt_buying = analysis.get("option_buying", {})
                    decision = opt_buying.get("decision", "WAIT")
                    score = opt_buying.get("setup_score", 0)
                    trade_plan = opt_buying.get("trade_plan", {})
                    
                    if decision in ("CE", "PE") and score >= 75 and trade_plan.get("entry_price", 0) > 0:
                        from app.services.options_signal_service import record_signal
                        sig_payload = {
                            "symbol": sym,
                            "type": decision,
                            "contract_name": trade_plan.get("contract_name"),
                            "strike": trade_plan.get("strike"),
                            "expiry": trade_plan.get("expiry"),
                            "spot_price": analysis.get("market_bias", {}).get("spot_price", 0.0),
                            "entry_price": trade_plan.get("entry_price"),
                            "stop_loss": trade_plan.get("stop_loss"),
                            "target_1": trade_plan.get("target_1"),
                            "target_2": trade_plan.get("target_2"),
                            "risk_reward": trade_plan.get("risk_reward", "1:2.0"),
                            "setup_score": score,
                            "reason": opt_buying.get("reason", "")
                        }
                        record_signal(sig_payload, is_paper_trade=False)
                except Exception as e:
                    log.warning(f"Background refresh for {sym} failed: {e}")
                time.sleep(2) # Slight stagger between symbols

            # Update live P&L and target/SL hits across all active signals
            try:
                from app.services.options_signal_service import update_active_signals
                update_active_signals()
            except Exception as e:
                log.warning(f"Error updating active signals P&L: {e}")
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
