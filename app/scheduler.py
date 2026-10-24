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
import hashlib
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, Tuple

from app.config import Config
from app.market_data import MarketDataProvider
from app.analysis import MarketAnalyzer
from app.strategies import IntradayEngine, PositionalEngine
from app.trading import PaperTradingManager
from app.models import OISnapshot, SignalCandidate
from app.extensions import db

log = logging.getLogger("scheduler")
IST = ZoneInfo("Asia/Kolkata")


class SignalDeduplicator:
    """
    Manages deterministic deduplication and heartbeat persistence for SignalCandidate records.
    Prevents high-frequency log inflation while preserving all material shifts in market structure,
    strikes, net credit, eligibility status, and rejection rationale across independent engines.
    """
    _cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    _lock = threading.Lock()

    @classmethod
    def reset_cache(cls):
        """Clears the in-memory deduplication cache."""
        with cls._lock:
            cls._cache.clear()

    @classmethod
    def compute_fingerprint(
        cls,
        engine_type: str,
        strategy_id: str,
        expiry: str,
        status: str,
        strikes: Tuple[Optional[float], Optional[float], Optional[float], Optional[float]],
        rationale: str,
    ) -> str:
        """
        Generates a deterministic SHA-256 fingerprint for a candidate's structural setup.
        """
        norm_engine = engine_type.upper().strip()
        norm_strategy = strategy_id.upper().strip()
        norm_expiry = expiry.upper().strip()
        norm_status = status.upper().strip()
        norm_rationale = " ".join((rationale or "").strip().split())
        payload = f"{norm_engine}|{norm_strategy}|{norm_expiry}|{norm_status}|{strikes}|{norm_rationale}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def should_persist_signal(
        cls,
        cand: Any,
        now_dt: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a candidate should be persisted or deduplicated.
        Returns (should_persist, reason).
        """
        if now_dt is None:
            now_dt = datetime.now(IST)

        engine_type = cand.engine_type.upper().strip()
        strategy_id = cand.strategy_id.strip()
        expiry = cand.expiry.strip()
        status = "ELIGIBLE" if getattr(cand, "is_eligible", False) else "WAIT"

        short_s = cand.legs[0].strike if getattr(cand, "legs", None) and len(cand.legs) > 0 else None
        long_s = cand.legs[1].strike if getattr(cand, "legs", None) and len(cand.legs) > 1 else None
        short_s2 = cand.legs[2].strike if getattr(cand, "legs", None) and len(cand.legs) > 2 else None
        long_s2 = cand.legs[3].strike if getattr(cand, "legs", None) and len(cand.legs) > 3 else None
        strikes = (short_s, long_s, short_s2, long_s2)

        norm_rationale = " ".join((getattr(cand, "rationale", "") or "").strip().split())
        net_credit = round(float(getattr(cand, "net_credit_pts", 0.0) or 0.0), 2)

        key = (engine_type, strategy_id, expiry)

        with cls._lock:
            # If not in memory, attempt database recovery to handle application restarts gracefully
            if key not in cls._cache:
                try:
                    last_sc = SignalCandidate.query.filter_by(
                        engine_type=engine_type,
                        strategy_id=strategy_id,
                        expiry=expiry,
                    ).order_by(SignalCandidate.id.desc()).first()

                    if last_sc and last_sc.created_at:
                        last_strikes = (
                            last_sc.short_strike,
                            last_sc.long_strike,
                            last_sc.short_strike_2,
                            last_sc.long_strike_2,
                        )
                        last_norm_rat = " ".join((last_sc.rationale or "").strip().split())
                        cls._cache[key] = {
                            "fingerprint": cls.compute_fingerprint(
                                last_sc.engine_type,
                                last_sc.strategy_id,
                                last_sc.expiry,
                                last_sc.status,
                                last_strikes,
                                last_norm_rat,
                            ),
                            "status": last_sc.status,
                            "strikes": last_strikes,
                            "net_credit_pts": round(float(last_sc.net_credit_pts or 0.0), 2),
                            "rationale": last_norm_rat,
                            "timestamp": last_sc.created_at,
                        }
                except Exception as e:
                    log.warning(f"Failed querying last SignalCandidate during deduplication: {e}")

            if key not in cls._cache:
                return True, "INITIAL_CANDIDATE"

            prev = cls._cache[key]

            # 1. Eligibility Status Change (ELIGIBLE <-> WAIT)
            if status != prev["status"]:
                return True, f"STATUS_CHANGED: {prev['status']} -> {status}"

            # 2. Contract Legs / Strike Change
            if strikes != prev["strikes"]:
                return True, f"STRIKES_CHANGED: {prev['strikes']} -> {strikes}"

            # 3. Meaningful Rejection Reason Change when status is WAIT
            if status == "WAIT" and norm_rationale != prev["rationale"]:
                return True, "REJECTION_REASON_CHANGED"

            # 4. Material Net Credit Change beyond tolerance
            credit_diff = abs(net_credit - prev["net_credit_pts"])
            tolerance = getattr(Config, "SIGNAL_NET_CREDIT_TOLERANCE_PTS", 0.5)
            if credit_diff > tolerance:
                return True, f"NET_CREDIT_SHIFT: {prev['net_credit_pts']:.2f} -> {net_credit:.2f} (delta {credit_diff:.2f} > {tolerance:.2f})"

            # 5. Heartbeat Interval Expiration (default 15 minutes)
            prev_ts = prev["timestamp"]
            if prev_ts.tzinfo is None and now_dt.tzinfo is not None:
                prev_ts = prev_ts.replace(tzinfo=now_dt.tzinfo)
            elif prev_ts.tzinfo is not None and now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=prev_ts.tzinfo)

            elapsed_seconds = (now_dt - prev_ts).total_seconds()
            heartbeat_seconds = getattr(Config, "SIGNAL_HEARTBEAT_MINS", 15) * 60.0

            if elapsed_seconds >= heartbeat_seconds:
                return True, f"HEARTBEAT_EXPIRED: {elapsed_seconds / 60.0:.1f}m >= {heartbeat_seconds / 60.0:.1f}m"

            # Otherwise, signal is identical within documented tolerance and inside heartbeat interval
            return False, "DEDUPLICATED_UNCHANGED"

    @classmethod
    def record_persisted_signal(
        cls,
        cand: Any,
        now_dt: Optional[datetime] = None,
    ):
        """
        Updates the in-memory cache tracking the most recently persisted signal state.
        """
        if now_dt is None:
            now_dt = datetime.now(IST)

        engine_type = cand.engine_type.upper().strip()
        strategy_id = cand.strategy_id.strip()
        expiry = cand.expiry.strip()
        status = "ELIGIBLE" if getattr(cand, "is_eligible", False) else "WAIT"

        short_s = cand.legs[0].strike if getattr(cand, "legs", None) and len(cand.legs) > 0 else None
        long_s = cand.legs[1].strike if getattr(cand, "legs", None) and len(cand.legs) > 1 else None
        short_s2 = cand.legs[2].strike if getattr(cand, "legs", None) and len(cand.legs) > 2 else None
        long_s2 = cand.legs[3].strike if getattr(cand, "legs", None) and len(cand.legs) > 3 else None
        strikes = (short_s, long_s, short_s2, long_s2)

        norm_rationale = " ".join((getattr(cand, "rationale", "") or "").strip().split())
        net_credit = round(float(getattr(cand, "net_credit_pts", 0.0) or 0.0), 2)
        fingerprint = cls.compute_fingerprint(engine_type, strategy_id, expiry, status, strikes, norm_rationale)

        key = (engine_type, strategy_id, expiry)
        with cls._lock:
            cls._cache[key] = {
                "fingerprint": fingerprint,
                "status": status,
                "strikes": strikes,
                "net_credit_pts": net_credit,
                "rationale": norm_rationale,
                "timestamp": now_dt,
            }


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
    def is_market_open(cls, dt: Optional[datetime] = None) -> bool:
        """Checks if current time is within Indian equity derivative market hours (Mon-Fri 09:15 - 15:30 IST)."""
        now = dt or datetime.now(IST)
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        time_str = now.strftime("%H:%M:%S")
        return "09:15:00" <= time_str <= "15:30:00"

    @classmethod
    def _run_loop(cls, interval_sec: int):
        while cls._running:
            try:
                with cls._app.app_context():
                    if cls.is_market_open() or cls._app.config.get("TESTING", False):
                        cls.run_cycle()
                    else:
                        log.debug("Market closed (Mon-Fri 09:15-15:30 IST). Scheduler idle.")
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

        # 5. Persist evaluated candidates (with deduplication & heartbeat)
        try:
            now_dt = datetime.now(IST)
            persisted_count = 0
            for cand in all_candidates:
                should_persist, reason = SignalDeduplicator.should_persist_signal(cand, now_dt=now_dt)
                if not should_persist:
                    continue

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
                SignalDeduplicator.record_persisted_signal(cand, now_dt=now_dt)
                persisted_count += 1

            if persisted_count > 0:
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
