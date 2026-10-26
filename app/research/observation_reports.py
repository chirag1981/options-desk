"""
app/research/observation_reports.py — Report Generator for Iron Condor Observation Ledger

Reconstructs complete daily and cumulative performance metrics strictly from the
underlying SQLite observation ledger:
- Sessions observed
- Trades, winning trades, losing trades, win rate
- Gross P&L and itemized statutory costs (Brokerage, STT, Exchange, GST, Stamp Duty)
- Net P&L after costs (Base vs Conservative)
- Profit Factor, Expectancy per trade
- Max drawdown, losing streak
- Count of rejected signals and reasons
- Count of data-quality failures (missing/stale/crossed quotes)
- Base vs. Conservative fill and slippage differences
"""

import sqlite3
import math
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.research.observation_models import get_db_connection, OBSERVATION_DB_PATH


class ObservationReportGenerator:
    """Generates auditable analytics from the observation ledger."""

    def __init__(self, db_path: str = OBSERVATION_DB_PATH):
        self.db_path = db_path

    def generate_cumulative_report(self) -> Dict[str, Any]:
        """Calculates cumulative observation statistics."""
        with get_db_connection(self.db_path) as conn:
            # 1. Closed Trades
            closed_trades = conn.execute("""
            SELECT * FROM observation_positions
            WHERE status = 'CLOSED'
            ORDER BY exit_time ASC
            """).fetchall()

            # 2. Open Trades
            open_count = conn.execute("SELECT COUNT(*) as cnt FROM observation_positions WHERE status = 'OPEN'").fetchone()["cnt"]

            # 3. Signals
            signals = conn.execute("SELECT status, COUNT(*) as cnt FROM observation_signals GROUP BY status").fetchall()
            sig_counts = {r["status"]: r["cnt"] for r in signals}
            total_signals = sum(sig_counts.values())
            eligible_signals = sig_counts.get("ELIGIBLE", 0)
            rejected_signals = sig_counts.get("REJECTED", 0)

            # 4. Data Quality Events
            dq_events = conn.execute("SELECT event_type, COUNT(*) as cnt FROM observation_data_quality_events GROUP BY event_type").fetchall()
            dq_counts = {r["event_type"]: r["cnt"] for r in dq_events}
            total_dq_failures = sum(dq_counts.values())

            # 5. Distinct Sessions Observed
            sessions = conn.execute("""
            SELECT DISTINCT substr(created_at, 1, 10) as session_date FROM observation_signals
            UNION
            SELECT DISTINCT substr(entry_time, 1, 10) as session_date FROM observation_positions
            """).fetchall()
            distinct_sessions = len(sessions)

            if not closed_trades:
                return {
                    "sessions_observed": distinct_sessions,
                    "total_signals_evaluated": total_signals,
                    "eligible_signals": eligible_signals,
                    "rejected_signals": rejected_signals,
                    "data_quality_events": dq_counts,
                    "total_data_quality_events": total_dq_failures,
                    "open_positions": open_count,
                    "total_closed_trades": 0,
                    "metrics": None,
                }

            # Calculate trade metrics
            total_trades = len(closed_trades)
            wins_base = [t for t in closed_trades if (t["net_pnl_inr"] or 0.0) > 0]
            losses_base = [t for t in closed_trades if (t["net_pnl_inr"] or 0.0) <= 0]
            win_rate_base = (len(wins_base) / total_trades) * 100.0

            gross_pnl_base = sum(t["gross_pnl_inr"] or 0.0 for t in closed_trades)
            total_costs = sum(t["total_costs_inr"] or 0.0 for t in closed_trades)
            net_pnl_base = sum(t["net_pnl_inr"] or 0.0 for t in closed_trades)
            brokerage = sum(t["brokerage_inr"] or 0.0 for t in closed_trades)
            stt = sum(t["stt_inr"] or 0.0 for t in closed_trades)
            exchange_fees = sum(t["exchange_fees_inr"] or 0.0 for t in closed_trades)
            gst = sum(t["gst_inr"] or 0.0 for t in closed_trades)
            stamp_duty = sum(t["stamp_duty_inr"] or 0.0 for t in closed_trades)
            sebi_ipft = sum(t["sebi_ipft_inr"] or 0.0 for t in closed_trades)

            # Profit Factor & Expectancy (Base)
            gross_profit_base = sum(t["net_pnl_inr"] for t in wins_base)
            gross_loss_base = abs(sum(t["net_pnl_inr"] for t in losses_base))
            profit_factor_base = round(gross_profit_base / gross_loss_base, 2) if gross_loss_base > 0 else 999.0
            expectancy_base = round(net_pnl_base / total_trades, 2)

            # Drawdown & Consecutive Losses
            cum_pnl = 0.0
            peak = 0.0
            max_drawdown = 0.0
            current_streak = 0
            max_losing_streak = 0

            for t in closed_trades:
                pnl = t["net_pnl_inr"] or 0.0
                cum_pnl += pnl
                if cum_pnl > peak:
                    peak = cum_pnl
                dd = peak - cum_pnl
                if dd > max_drawdown:
                    max_drawdown = dd

                if pnl <= 0:
                    current_streak += 1
                    if current_streak > max_losing_streak:
                        max_losing_streak = current_streak
                else:
                    current_streak = 0

            # Conservative Comparison
            net_pnl_cons = sum(t["conservative_net_pnl_inr"] or 0.0 for t in closed_trades)
            wins_cons = [t for t in closed_trades if (t["conservative_net_pnl_inr"] or 0.0) > 0]
            losses_cons = [t for t in closed_trades if (t["conservative_net_pnl_inr"] or 0.0) <= 0]
            win_rate_cons = (len(wins_cons) / total_trades) * 100.0
            gross_profit_cons = sum(t["conservative_net_pnl_inr"] for t in wins_cons)
            gross_loss_cons = abs(sum(t["conservative_net_pnl_inr"] for t in losses_cons))
            profit_factor_cons = round(gross_profit_cons / gross_loss_cons, 2) if gross_loss_cons > 0 else 999.0
            expectancy_cons = round(net_pnl_cons / total_trades, 2)
            slippage_impact = sum(t["conservative_slippage_cost_inr"] or 0.0 for t in closed_trades)

            return {
                "sessions_observed": distinct_sessions,
                "total_signals_evaluated": total_signals,
                "eligible_signals": eligible_signals,
                "rejected_signals": rejected_signals,
                "data_quality_events": dq_counts,
                "total_data_quality_events": total_dq_failures,
                "open_positions": open_count,
                "total_closed_trades": total_trades,
                "base_scenario": {
                    "wins": len(wins_base),
                    "losses": len(losses_base),
                    "win_rate_pct": round(win_rate_base, 1),
                    "gross_pnl_inr": round(gross_pnl_base, 2),
                    "total_costs_inr": round(total_costs, 2),
                    "net_pnl_inr": round(net_pnl_base, 2),
                    "profit_factor": profit_factor_base,
                    "expectancy_inr": expectancy_base,
                    "max_drawdown_inr": round(max_drawdown, 2),
                    "max_losing_streak": max_losing_streak,
                    "cost_breakdown": {
                        "brokerage": round(brokerage, 2),
                        "stt": round(stt, 2),
                        "exchange_fees": round(exchange_fees, 2),
                        "gst": round(gst, 2),
                        "stamp_duty": round(stamp_duty, 2),
                        "sebi_ipft": round(sebi_ipft, 2),
                    }
                },
                "conservative_scenario": {
                    "wins": len(wins_cons),
                    "losses": len(losses_cons),
                    "win_rate_pct": round(win_rate_cons, 1),
                    "net_pnl_inr": round(net_pnl_cons, 2),
                    "profit_factor": profit_factor_cons,
                    "expectancy_inr": expectancy_cons,
                    "slippage_drag_inr": round(slippage_impact, 2),
                }
            }

    def generate_daily_summary(self) -> List[Dict[str, Any]]:
        """Produces date-by-date daily trade summaries."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute("""
            SELECT 
                substr(exit_time, 1, 10) as trade_date,
                COUNT(*) as trades,
                SUM(CASE WHEN net_pnl_inr > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN net_pnl_inr <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(gross_pnl_inr) as gross_pnl,
                SUM(total_costs_inr) as total_costs,
                SUM(net_pnl_inr) as net_pnl,
                SUM(conservative_net_pnl_inr) as cons_net_pnl
            FROM observation_positions
            WHERE status = 'CLOSED'
            GROUP BY substr(exit_time, 1, 10)
            ORDER BY trade_date ASC
            """).fetchall()

            return [dict(r) for r in rows]
