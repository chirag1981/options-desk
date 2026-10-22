"""
app/risk.py — Authoritative Portfolio Risk Guard & Engine Limits
Enforces:
1. Hard Single-Trade Risk Limit: Max 0.5% account equity.
2. Hard Combined Portfolio Risk Ceiling: Max 2.0% total account equity across both engines.
3. Daily Drawdown Circuit Breaker: Max 1.5% daily realized loss halts fresh entries.
4. Engine-Specific Position Limits: Max 2 concurrent positions per engine.
5. Consecutive Loss Lockout: 15-minute pause after stop-loss exits.
6. Zero Naked Exposure Invariant: Aborts immediately if any short leg lacks protection.
"""

import threading
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional

from app.config import Config
from app.strategies import SpreadCandidate
from app.models import TradePosition, RiskAuditLog
from app.extensions import db

log = logging.getLogger("risk")
IST = ZoneInfo("Asia/Kolkata")
_risk_lock = threading.Lock()


@dataclass
class RiskEvaluationResult:
    """Standardized decision returned by PortfolioRiskManager."""
    approved: bool
    engine_type: str
    trade_risk_inr: float
    current_portfolio_risk_inr: float
    projected_portfolio_risk_inr: float
    max_allowed_risk_inr: float
    daily_realized_loss_inr: float
    rejection_reasons: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return "APPROVED" if self.approved else f"BLOCKED: {'; '.join(self.rejection_reasons)}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "engine_type": self.engine_type,
            "trade_risk_inr": self.trade_risk_inr,
            "current_portfolio_risk_inr": self.current_portfolio_risk_inr,
            "projected_portfolio_risk_inr": self.projected_portfolio_risk_inr,
            "max_allowed_risk_inr": self.max_allowed_risk_inr,
            "daily_realized_loss_inr": self.daily_realized_loss_inr,
            "rejection_reasons": self.rejection_reasons,
        }


class PortfolioRiskManager:
    """
    Authoritative Thread-Safe Singleton Portfolio Risk Manager.
    """
    _last_loss_exit_time: Optional[datetime] = None

    @classmethod
    def register_loss_exit(cls, exit_time: Optional[datetime] = None):
        with _risk_lock:
            cls._last_loss_exit_time = exit_time or datetime.now(IST)
            log.info(f"Loss exit registered at {cls._last_loss_exit_time}. 15-minute cooldown initiated.")

    @classmethod
    def evaluate_candidate_risk(
        cls,
        candidate: SpreadCandidate,
        engine_type: str = "INTRADAY",
        now_dt: Optional[datetime] = None,
    ) -> RiskEvaluationResult:
        """
        Validates proposed candidate spread against portfolio and engine risk limits.
        """
        if now_dt is None:
            now_dt = datetime.now(IST)

        engine_type = engine_type.upper().strip()
        rejections = []

        total_equity = Config.TOTAL_ACCOUNT_EQUITY
        max_per_trade_risk = (Config.MAX_RISK_PER_TRADE_PCT / 100.0) * total_equity
        max_combined_risk = (Config.MAX_COMBINED_PORTFOLIO_RISK_PCT / 100.0) * total_equity
        max_daily_loss = (Config.MAX_DAILY_LOSS_PCT / 100.0) * total_equity

        with _risk_lock:
            # 1. Structural Invariant: Zero naked options permitted
            if candidate.has_uncovered_short:
                rejections.append("CRITICAL: Uncovered short option detected. Naked options strictly forbidden.")

            trade_risk = candidate.max_loss_inr
            if trade_risk <= 0:
                rejections.append(f"Invalid maximum loss calculation ({trade_risk:.2f} INR).")
            elif trade_risk > max_per_trade_risk:
                rejections.append(
                    f"Trade risk (₹{trade_risk:,.2f}) exceeds {Config.MAX_RISK_PER_TRADE_PCT}% limit (₹{max_per_trade_risk:,.2f})."
                )

            # 2. Consecutive Loss Cooldown Guard
            if cls._last_loss_exit_time:
                cooldown_delta = now_dt - cls._last_loss_exit_time
                if cooldown_delta < timedelta(minutes=Config.CONSECUTIVE_LOSS_COOLDOWN_MINS):
                    remaining_mins = Config.CONSECUTIVE_LOSS_COOLDOWN_MINS - int(cooldown_delta.total_seconds() / 60)
                    rejections.append(f"Consecutive loss cooldown active ({remaining_mins} mins remaining).")

            # 3. Query Active Positions and Daily Realized Loss from Database
            current_portfolio_risk = 0.0
            daily_realized_loss = 0.0
            active_engine_positions = 0

            try:
                today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                
                # Active open positions
                open_positions = TradePosition.query.filter_by(status="OPEN").all()
                for pos in open_positions:
                    current_portfolio_risk += pos.max_loss_inr
                    if pos.engine_type.upper() == engine_type:
                        active_engine_positions += 1

                # Daily closed positions with realized loss
                closed_today = TradePosition.query.filter(
                    TradePosition.status.in_(["CLOSED", "STOPPED_OUT", "TARGET_HIT", "SESSION_EXIT"]),
                    TradePosition.exit_time >= today_start,
                ).all()

                for pos in closed_today:
                    if pos.net_pnl_inr < 0:
                        daily_realized_loss += abs(pos.net_pnl_inr)

            except Exception as e:
                log.warning(f"Database query in risk evaluation failed: {e}")

            # 4. Engine Concurrent Position Cap
            max_positions = Config.MAX_INTRADAY_POSITIONS if engine_type == "INTRADAY" else Config.MAX_POSITIONAL_POSITIONS
            if active_engine_positions >= max_positions:
                rejections.append(
                    f"{engine_type} engine has reached maximum concurrent positions limit ({max_positions})."
                )

            # 5. Combined Portfolio Risk Ceiling
            projected_risk = current_portfolio_risk + trade_risk
            if projected_risk > max_combined_risk:
                rejections.append(
                    f"Projected portfolio risk (₹{projected_risk:,.2f}) exceeds 2.0% ceiling (₹{max_combined_risk:,.2f})."
                )

            # 6. Daily Drawdown Circuit Breaker
            if daily_realized_loss >= max_daily_loss:
                rejections.append(
                    f"Daily realized loss (₹{daily_realized_loss:,.2f}) breached 1.5% limit (₹{max_daily_loss:,.2f}). Trading halted for the day."
                )

            approved = (len(rejections) == 0)

            # Audit Logging
            try:
                audit = RiskAuditLog(
                    engine_type=engine_type,
                    event_type="TRADE_APPROVED" if approved else "TRADE_BLOCKED",
                    decision="APPROVED" if approved else "BLOCKED",
                    trade_risk_inr=trade_risk,
                    current_portfolio_risk_inr=current_portfolio_risk,
                    reason="All pre-entry risk constraints satisfied." if approved else "; ".join(rejections),
                )
                db.session.add(audit)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                log.warning(f"Risk audit logging error: {e}")

            return RiskEvaluationResult(
                approved=approved,
                engine_type=engine_type,
                trade_risk_inr=trade_risk,
                current_portfolio_risk_inr=current_portfolio_risk,
                projected_portfolio_risk_inr=projected_risk,
                max_allowed_risk_inr=max_combined_risk,
                daily_realized_loss_inr=daily_realized_loss,
                rejection_reasons=rejections,
            )
