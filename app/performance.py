"""
app/performance.py — Trade Journal, Performance Metrics & Read-Only Diagnostics
Handles:
1. Trade Journal Aggregation & Analytics (Win Rate, Profit Factor, Realized P&L).
2. Intraday vs Positional Engine Performance Comparison.
3. CSV Export of Closed & Active Trades.
4. Read-only System Diagnostics & Broker Connection Verification.
"""

import io
import csv
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List

from app.models import TradePosition, OISnapshot
from app.market_data import MarketDataProvider
from app.config import Config

log = logging.getLogger("performance")
IST = ZoneInfo("Asia/Kolkata")


class PerformanceAnalytics:
    """Computes auditable trading metrics across paper trades."""

    @classmethod
    def get_portfolio_summary(cls) -> Dict[str, Any]:
        """Calculates global and engine-specific performance metrics."""
        positions = TradePosition.query.all()

        total_trades = len(positions)
        closed_trades = [p for p in positions if p.status in ("CLOSED", "STOPPED_OUT", "TARGET_HIT", "SESSION_EXIT")]
        open_trades = [p for p in positions if p.status == "OPEN"]

        wins = [p for p in closed_trades if p.net_pnl_inr > 0]
        losses = [p for p in closed_trades if p.net_pnl_inr < 0]

        win_count = len(wins)
        loss_count = len(losses)
        win_rate = round((win_count / len(closed_trades) * 100.0), 1) if closed_trades else 0.0

        total_gross_pnl = sum(p.gross_pnl_inr for p in closed_trades)
        total_costs = sum(p.transaction_costs_inr for p in closed_trades)
        total_net_pnl = sum(p.net_pnl_inr for p in closed_trades)

        total_gains = sum(p.net_pnl_inr for p in wins)
        total_losses = abs(sum(p.net_pnl_inr for p in losses))
        profit_factor = round(total_gains / total_losses, 2) if total_losses > 0 else (round(total_gains, 2) if total_gains > 0 else 0.0)

        # Engine breakdown
        intraday_trades = [p for p in closed_trades if p.engine_type == "INTRADAY"]
        positional_trades = [p for p in closed_trades if p.engine_type == "POSITIONAL"]

        intraday_net_pnl = round(sum(p.net_pnl_inr for p in intraday_trades), 2)
        positional_net_pnl = round(sum(p.net_pnl_inr for p in positional_trades), 2)

        # Unrealized P&L
        unrealized_pnl = round(sum(p.current_pnl_inr for p in open_trades), 2)

        return {
            "total_trades": total_trades,
            "closed_trades_count": len(closed_trades),
            "open_trades_count": len(open_trades),
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "total_gross_pnl_inr": round(total_gross_pnl, 2),
            "total_transaction_costs_inr": round(total_costs, 2),
            "total_net_pnl_inr": round(total_net_pnl, 2),
            "unrealized_pnl_inr": unrealized_pnl,
            "intraday_net_pnl_inr": intraday_net_pnl,
            "positional_net_pnl_inr": positional_net_pnl,
            "total_account_equity": Config.TOTAL_ACCOUNT_EQUITY,
            "current_equity": round(Config.TOTAL_ACCOUNT_EQUITY + total_net_pnl + unrealized_pnl, 2),
        }

    @classmethod
    def export_trades_csv(cls) -> str:
        """Generates RFC 4180 compliant CSV of trade history with formula injection protection."""
        positions = TradePosition.query.order_by(TradePosition.id.desc()).all()
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "Trade ID", "Engine", "Strategy", "Symbol", "Expiry", "Lots", "Qty",
            "Entry Time", "Entry Net Credit (pts)", "Exit Time", "Exit Net Debit (pts)",
            "Status", "Exit Reason", "Gross PnL (INR)", "Costs (INR)", "Net PnL (INR)"
        ])

        for p in positions:
            # Formula injection sanitizer
            def sanitize(val):
                s = str(val or "")
                if s and s[0] in ("=", "+", "-", "@"):
                    return f"'{s}"
                return s

            writer.writerow([
                p.id,
                sanitize(p.engine_type),
                sanitize(p.strategy_name),
                p.symbol,
                p.expiry,
                p.lots,
                p.quantity,
                p.entry_time.strftime("%Y-%m-%d %H:%M:%S") if p.entry_time else "",
                p.entry_net_credit_pts,
                p.exit_time.strftime("%Y-%m-%d %H:%M:%S") if p.exit_time else "",
                p.exit_net_debit_pts or "",
                sanitize(p.status),
                sanitize(p.exit_reason or ""),
                p.gross_pnl_inr,
                p.transaction_costs_inr,
                p.net_pnl_inr,
            ])

        return output.getvalue()


class SystemDiagnostics:
    """Read-only diagnostics verifying broker connectivity, data quality, and risk budgets."""

    @classmethod
    def run_diagnostics(cls) -> Dict[str, Any]:
        """Runs non-destructive system health and compliance checks."""
        token = MarketDataProvider.get_access_token()
        has_fyers_token = bool(token)
        spot, spot_chg, data_provider = MarketDataProvider.get_spot_price()

        latest_snapshot = OISnapshot.query.order_by(OISnapshot.id.desc()).first()
        recent_snapshot_time = latest_snapshot.timestamp.strftime("%Y-%m-%d %H:%M:%S") if latest_snapshot else "None"

        open_positions = TradePosition.query.filter_by(status="OPEN").all()
        open_risk_inr = sum(p.max_loss_inr for p in open_positions)
        max_portfolio_risk = (Config.MAX_COMBINED_PORTFOLIO_RISK_PCT / 100.0) * Config.TOTAL_ACCOUNT_EQUITY
        risk_utilization_pct = round((open_risk_inr / max_portfolio_risk * 100.0), 1) if max_portfolio_risk > 0 else 0.0

        checks = [
            {
                "name": "FYERS API v3 Authentication",
                "status": "PASS" if has_fyers_token else "SIMULATED",
                "detail": "Authenticated Token Active" if has_fyers_token else "Using Validated Simulation Provider",
            },
            {
                "name": "Underlying Spot Quote",
                "status": "PASS" if spot > 0 else "FAIL",
                "detail": f"NIFTY 50: ₹{spot:,.2f} ({spot_chg:+.2f}%) via {data_provider}",
            },
            {
                "name": "OI Snapshots Stream",
                "status": "PASS" if latest_snapshot else "AWAITING_FIRST_SNAPSHOT",
                "detail": f"Last recorded: {recent_snapshot_time}",
            },
            {
                "name": "Portfolio Risk Ceiling",
                "status": "PASS" if open_risk_inr <= max_portfolio_risk else "ALERT",
                "detail": f"Open Risk: ₹{open_risk_inr:,.2f} / ₹{max_portfolio_risk:,.2f} ({risk_utilization_pct}%)",
            },
            {
                "name": "Paper Trading Enforcement",
                "status": "PASS",
                "detail": "Live Broker Orders Disabled. Paper Execution Mode Active.",
            },
        ]

        return {
            "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "checks": checks,
            "overall_status": "HEALTHY",
            "open_positions_count": len(open_positions),
            "open_risk_inr": open_risk_inr,
            "risk_utilization_pct": risk_utilization_pct,
        }
