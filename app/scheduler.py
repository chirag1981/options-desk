"""
app/scheduler.py — Background Market Poller & Session Lifecycle Engine
Handles:
1. Periodic Market Data & Option Chain polling.
2. Open Interest Snapshot persistence in SQLite.
3. Dual Engine Candidate Evaluation (IntradayEngine & PositionalEngine).
4. Mark-to-Market (MTM) updates for open positions.
5. Intraday Session Cutoff (15:20 IST) automatic position closure.
"""

import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from app.config import Config
from app.market_data import MarketDataProvider
from app.analysis import MarketAnalyzer
from app.strategies import IntradayEngine, PositionalEngine
from app.trading import PaperTradingManager
from app.models import OISnapshot, SignalCandidate
from app.extensions import db

log = logging.getLogger("scheduler")
IST = ZoneInfo("Asia/Kolkata")


class DeskScheduler:
    """
    Controlled background scheduler executing periodic market analysis and position management.
    """
    _thread: Optional[threading.Thread] = None
    _running: bool = False
    _app = None

    @classmethod
    def start(cls, app, poll_interval_sec: int = 15):
        """Starts the scheduler background thread with Flask app context."""
        if cls._thread and cls._thread.is_alive():
            log.info("DeskScheduler already running.")
            return

        cls._app = app
        cls._running = True
        cls._thread = threading.Thread(
            target=cls._run_loop,
            args=(poll_interval_sec,),
            daemon=True,
            name="OptionsDeskSchedulerThread",
        )
        cls._thread.start()
        log.info(f"DeskScheduler started with {poll_interval_sec}s polling interval.")

    @classmethod
    def stop(cls):
        """Stops the scheduler loop."""
        cls._running = False
        log.info("DeskScheduler stopping.")

    @classmethod
    def _run_loop(cls, interval_sec: int):
        while cls._running:
            try:
                with cls._app.app_context():
                    cls.run_cycle()
            except Exception as e:
                log.error(f"Error in DeskScheduler cycle: {e}")

            time.sleep(interval_sec)

    @classmethod
    def run_cycle(cls):
        """Executes a single market analysis, candidate generation, and MTM update cycle."""
        # 1. Fetch live or validated simulated option chain
        chain_data = MarketDataProvider.get_option_chain()
        spot = chain_data.get("spot_price", 0.0)
        chain = chain_data.get("chain_by_strike", {})

        if spot <= 0 or not chain:
            return

        # 2. Analyze market structure and classify regime
        analysis = MarketAnalyzer.analyze_chain(chain_data)

        # 3. Persist timestamped OI snapshot
        try:
            snapshot = OISnapshot(
                symbol=analysis.symbol,
                expiry=analysis.expiry,
                spot_price=analysis.spot_price,
                atm_strike=analysis.atm_strike,
                s1_support=analysis.s1_support,
                s2_support=analysis.s2_support,
                r1_resistance=analysis.r1_resistance,
                r2_resistance=analysis.r2_resistance,
                pcr_oi=analysis.pcr_oi,
                total_pe_oi=analysis.total_pe_oi,
                total_ce_oi=analysis.total_ce_oi,
                total_pe_change_oi=analysis.total_pe_change_oi,
                total_ce_change_oi=analysis.total_ce_change_oi,
                net_delta_pe_ce_oi=analysis.net_delta_pe_ce_oi,
                writer_momentum=analysis.writer_momentum,
                market_regime=analysis.market_regime,
                market_bias=analysis.market_bias,
                atm_iv=analysis.atm_iv,
            )
            db.session.add(snapshot)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            log.warning(f"Failed persisting OI snapshot: {e}")

        # 4. Evaluate Dual Trading Engines
        intraday_candidates = IntradayEngine.evaluate(analysis, chain)
        positional_candidates = PositionalEngine.evaluate(analysis, chain)

        all_candidates = intraday_candidates + positional_candidates

        # 5. Persist evaluated candidates
        try:
            for cand in all_candidates:
                short_s = cand.legs[0].strike if cand.legs else None
                long_s = cand.legs[1].strike if len(cand.legs) > 1 else None
                short_s2 = cand.legs[2].strike if len(cand.legs) > 2 else None
                long_s2 = cand.legs[3].strike if len(cand.legs) > 3 else None

                sc = SignalCandidate(
                    engine_type=cand.engine_type,
                    strategy_id=cand.strategy_id,
                    strategy_name=cand.strategy_name,
                    status="ELIGIBLE" if cand.is_eligible else "WAIT",
                    symbol=cand.symbol,
                    expiry=cand.expiry,
                    spot_at_eval=spot,
                    short_strike=short_s,
                    long_strike=long_s,
                    short_strike_2=short_s2,
                    long_strike_2=long_s2,
                    net_credit_pts=cand.net_credit_pts,
                    max_profit_inr=cand.max_profit_inr,
                    max_loss_inr=cand.max_loss_inr,
                    risk_reward_ratio=cand.risk_reward_ratio,
                    confidence_score=cand.confidence_score,
                    rationale=cand.rationale,
                )
                db.session.add(sc)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            log.warning(f"Failed persisting candidates: {e}")

        # 6. Mark-to-Market Valuation & Stop-Loss / Cutoff Monitoring
        PaperTradingManager.update_open_positions_mtm(chain, spot_price=spot)

        # 7. Automatic Strategy Execution (if auto-trading is active)
        try:
            from app.trading import AutoTradeEngine
            AutoTradeEngine.process_auto_trades(analysis, chain)
        except Exception as e:
            log.warning(f"Auto-trade engine cycle error: {e}")
