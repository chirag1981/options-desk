"""
scripts/run_reconciled_research_matrix.py — Authoritative Research Runner for Iron Condor Matrix

Conditions strictly enforced:
1. Research runner only: Zero modifications to production code, active settings, risk limits, or DB.
2. Zero live broker orders: Offline historical parquet files only.
3. Both baselines reconciled: 21-trade (100-pt wing) and 31-trade (50-pt wing).
4. Strictly enforces theoretical loss + estimated round-trip costs <= INR 5,000 per trade.
5. March-October 2026 labeled as previously examined test data, not untouched holdout.
6. Runs ADX, wing-width, target, and stop experiments one factor at a time.
7. Saves machine-readable JSON to instance/research_reports/reconciled_research_matrix.json.
"""

import os
import sys
import glob
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np

# Safe stdout encoding for Windows cp1252
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from app.backtesting.backtest_engine import (
    HistoricalBacktestEngine,
    BacktestTrade,
    StrategyMetrics,
    STARTING_EQUITY,
)
from app.backtesting.data_validator import HistoricalDataValidator
from app.research.regime_diagnostics import RegimeDiagnosticsEngine
from app.contracts import ContractRegistry
from app.strategies import StrategyBuilder, AutoStrategySelector
from app.analysis import MarketAnalyzer
from app.trading import TransactionCostEngine
from app.config import Config


@dataclass
class ResearchScenarioMetrics:
    scenario_id: str
    scenario_name: str
    category: str  # BASELINE, ADX_FILTER, WING_WIDTH, TARGET_SWEEP, STOP_SWEEP
    wing_width: float
    target_profit_pct: float
    stop_loss_loss_mult: float
    stop_loss_debit_mult: float
    adx_filter_active: bool
    adx_threshold: float
    
    # Portfolio Performance
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    gross_pnl: float
    total_brokerage: float
    total_taxes: float
    total_costs: float
    total_slippage: float
    net_pnl: float
    roi_pct: float
    profit_factor: float
    expectancy_inr: float
    max_drawdown_inr: float
    max_drawdown_pct: float
    largest_win_inr: float
    largest_loss_inr: float
    max_consecutive_losses: int
    
    # Chronological Split (Discovery Sessions 1-75 vs Previously Examined Sessions 76-105)
    discovery_trades: int
    discovery_net_pnl: float
    discovery_win_rate_pct: float
    discovery_profit_factor: float
    test_split_trades: int
    test_split_net_pnl: float
    test_split_win_rate_pct: float
    test_split_profit_factor: float
    
    # Candidate Filter Stats
    evaluated_candidates: int
    rejected_candidates: int
    rejection_reasons: Dict[str, int]
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ResearchBacktestEngine(HistoricalBacktestEngine):
    """
    Subclasses HistoricalBacktestEngine to allow single-factor parameter injection
    (wing_width, causal ADX pre-entry filter) while preserving 100% of the production
    portfolio execution logic, position limits, cooldown, and dated transaction costs.
    """

    def __init__(
        self,
        wing_width: float = 100.0,
        adx_filter_active: bool = False,
        adx_threshold: float = 22.0,
        daily_spot_df: Optional[pd.DataFrame] = None,
        cached_snapshots: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.wing_width = wing_width
        self.adx_filter_active = adx_filter_active
        self.adx_threshold = adx_threshold
        self.daily_spot_df = daily_spot_df
        self.cached_snapshots = cached_snapshots
        self.rejection_summary: Dict[str, int] = {}
        self.evaluated_candidates = 0
        self.rejected_candidates = 0

    def _record_rejection(self, reason: str):
        self.rejected_candidates += 1
        self.rejection_summary[reason] = self.rejection_summary.get(reason, 0) + 1

    def _get_causal_adx(self, trade_date: str, period: int = 14) -> Optional[float]:
        if self.daily_spot_df is None:
            return None
        prior_df = self.daily_spot_df[self.daily_spot_df["date"] < trade_date]
        if len(prior_df) < (period * 2):
            return None
        adx_series = RegimeDiagnosticsEngine.calculate_adx(
            high=prior_df["high"],
            low=prior_df["low"],
            close=prior_df["close"],
            period=period
        )
        val = adx_series.iloc[-1]
        return float(val) if not pd.isna(val) else None

    def _run_single_scenario(
        self,
        parquet_files: List[str],
        scenario: str
    ) -> Dict[str, StrategyMetrics]:
        """
        Executes single pass over chronological historical datasets with research parameters.
        """
        metrics: Dict[str, StrategyMetrics] = {
            s: StrategyMetrics(strategy_id=s, scenario=scenario)
            for s in ["IRON_CONDOR", "COMBINED"]
        }

        current_equity = self.starting_equity
        open_trades: List[BacktestTrade] = []
        consecutive_losses = 0
        cooldown_active_until = None
        current_trade_day = None
        daily_loss_incurred = 0.0
        circuit_breaker_active = False
        trade_counter = 0

        snapshots = self.cached_snapshots
        if snapshots is None:
            all_records = []
            for pf in parquet_files:
                try:
                    df = pd.read_parquet(pf)
                    valid, _, recs = HistoricalDataValidator.validate_dataframe(df, file_path=pf)
                    if valid:
                        all_records.extend(recs)
                except Exception:
                    pass
            snapshots, _, _, _, _, _, _ = HistoricalDataValidator.reconstruct_snapshots(all_records)

        last_snap = None

        for snap in snapshots:
            ts_str = snap["timestamp"]
            dt_obj = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            trade_date = dt_obj.date()
            date_str = str(trade_date)
            time_str = dt_obj.strftime("%H:%M:%S")
            expiry = snap["expiry"]
            spot = snap["spot_price"]
            chain = snap["chain_by_strike"]

            # 1. Day transition: settle open trades from previous session
            if current_trade_day is not None and current_trade_day != trade_date:
                expired_trades = [t for t in open_trades if t.expiry != expiry or t.entry_time.split()[0] != str(trade_date)]
                for et in expired_trades:
                    target_snap = last_snap or snap
                    self._close_trade_at_settlement(et, target_snap, scenario, reason="SESSION_CUTOFF")
                    current_equity += et.net_pnl
                    open_trades.remove(et)
                    self._record_trade(metrics[et.strategy_id], et)
                    self._record_trade(metrics["COMBINED"], et)

                current_trade_day = trade_date
                daily_loss_incurred = 0.0
                circuit_breaker_active = False
                cooldown_active_until = None
            elif current_trade_day is None:
                current_trade_day = trade_date

            last_snap = snap

            # 2. Monitor open positions
            closed_trades_this_tick = []
            for trade in open_trades:
                exit_cost_pts = self._evaluate_spread_exit_cost(
                    trade, chain, scenario, final_spot=spot, is_expiry_settlement=False
                )
                if exit_cost_pts is None:
                    continue

                exit_reason = None
                target_cost_threshold = round(trade.net_credit_pts * (1.0 - self.target_profit_pct), 2)
                stop_cost_threshold = round(trade.net_credit_pts * (1.0 + self.stop_loss_credit_mult), 2)

                if exit_cost_pts <= target_cost_threshold:
                    exit_reason = "TARGET"
                elif exit_cost_pts >= stop_cost_threshold:
                    exit_reason = "STOP_LOSS"
                elif time_str >= "15:20:00":
                    exit_reason = "CUTOFF"

                if exit_reason and exit_cost_pts is not None:
                    trade.exit_time = ts_str
                    trade.exit_reason = exit_reason
                    trade.exit_spread_cost = exit_cost_pts
                    try:
                        t_in = datetime.strptime(trade.entry_time, "%Y-%m-%d %H:%M:%S")
                        t_out = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        trade.holding_time_mins = round((t_out - t_in).total_seconds() / 60.0, 1)
                    except Exception:
                        trade.holding_time_mins = 0.0

                    gross_pnl = (trade.net_credit_pts - exit_cost_pts) * trade.quantity
                    trade.gross_pnl = round(gross_pnl, 2)

                    exit_legs = []
                    if exit_reason in ("TARGET", "STOP_LOSS", "CUTOFF"):
                        strikes_needed = [(trade.short_strike, "PE", "BUY"), (trade.long_strike, "PE", "SELL")]
                        if trade.short_strike_2 and trade.long_strike_2:
                            strikes_needed.extend([(trade.short_strike_2, "CE", "BUY"), (trade.long_strike_2, "CE", "SELL")])

                        for stk, opt, act in strikes_needed:
                            if stk in chain and chain[stk].get(opt):
                                p = self._get_contract_price_for_scenario(chain[stk][opt], act, scenario)
                            else:
                                p = exit_cost_pts / len(strikes_needed)
                            exit_legs.append({"action": act, "price": p, "quantity": trade.quantity})
                    else:
                        exit_legs = []

                    if exit_legs:
                        exit_costs = TransactionCostEngine.calculate_order_costs_breakdown(
                            exit_legs, lots=trade.lots, trade_date=trade_date
                        )
                        exit_brokerage = exit_costs["brokerage"]
                        exit_taxes = exit_costs["total"] - exit_brokerage
                    else:
                        exit_brokerage = 0.0
                        exit_taxes = 0.0

                    trade.total_brokerage += exit_brokerage
                    trade.total_taxes += exit_taxes
                    trade.total_costs = round(trade.total_brokerage + trade.total_taxes, 2)

                    num_legs = 4 if trade.strategy_id == "IRON_CONDOR" else 2
                    slippage_rate = 0.00 if scenario == "OPTIMISTIC" else 0.05 if scenario == "BASE" else 0.10
                    trade.slippage_cost = round(slippage_rate * num_legs * trade.quantity, 2)

                    trade.net_pnl = round(trade.gross_pnl - trade.total_costs, 2)

                    current_equity += trade.net_pnl
                    if trade.net_pnl < 0:
                        daily_loss_incurred += abs(trade.net_pnl)
                        consecutive_losses += 1
                        cooldown_active_until = dt_obj + timedelta(minutes=Config.CONSECUTIVE_LOSS_COOLDOWN_MINS)
                    else:
                        consecutive_losses = 0

                    if daily_loss_incurred >= Config.get_max_daily_loss_inr():
                        circuit_breaker_active = True

                    closed_trades_this_tick.append(trade)

            for ct in closed_trades_this_tick:
                open_trades.remove(ct)
                self._record_trade(metrics[ct.strategy_id], ct)
                self._record_trade(metrics["COMBINED"], ct)

            # 3. Entry Evaluation
            # Strict one-position invariant: only 1 open Iron Condor allowed
            if any(t.strategy_id == "IRON_CONDOR" for t in open_trades):
                continue
            if circuit_breaker_active:
                self._record_rejection("DAILY_CIRCUIT_BREAKER_ACTIVE")
                continue
            if cooldown_active_until and dt_obj < cooldown_active_until:
                self._record_rejection("CONSECUTIVE_LOSS_COOLDOWN_ACTIVE")
                continue
            if time_str < "09:20:00" or time_str > "14:45:00":
                continue

            self.evaluated_candidates += 1

            chain_data = {
                "symbol": "NIFTY",
                "spot_price": spot,
                "spot_change_pct": snap.get("spot_change_pct") or 0.0,
                "expiry": expiry,
                "timestamp": ts_str,
                "chain_by_strike": chain,
                "data_status": "HISTORICAL_REPLAY",
            }

            try:
                analysis = MarketAnalyzer.analyze_chain(chain_data)
                
                # Causal ADX Pre-Entry Filter (Single-factor evaluation)
                if self.adx_filter_active:
                    adx_val = self._get_causal_adx(date_str)
                    if adx_val is not None and adx_val >= self.adx_threshold:
                        self._record_rejection(f"ADX({adx_val:.1f})_EXCEEDS_{self.adx_threshold}")
                        continue

                # Pass research wing_width to evaluate_auto_selection
                selection = AutoStrategySelector.evaluate_auto_selection(
                    analysis, chain, wing_width=self.wing_width
                )

                if selection.selection_type != "NONE" and selection.selected_strategies:
                    for cand in selection.selected_strategies:
                        if cand.strategy_id not in self.approved_strategies:
                            continue
                        if not cand.is_eligible or cand.net_credit_pts <= 0:
                            self._record_rejection("; ".join(cand.rejection_reasons) if cand.rejection_reasons else "INELIGIBLE_CREDIT")
                            continue

                        if any(t.strategy_id == cand.strategy_id for t in open_trades):
                            continue

                        lot_size = ContractRegistry.get_contract_lot_size("NIFTY", expiry)
                        quantity = lot_size * cand.lots

                        legs_costs = []
                        valid_fill = True
                        for leg in cand.legs:
                            stk = leg.strike
                            opt = leg.option_type
                            act = leg.action
                            if stk not in chain or not chain[stk].get(opt):
                                valid_fill = False
                                break
                            p = self._get_contract_price_for_scenario(chain[stk][opt], act, scenario)
                            legs_costs.append({"action": act, "price": p, "quantity": quantity})

                        if not valid_fill or not legs_costs:
                            self._record_rejection("MISSING_LEG_QUOTE")
                            continue

                        sell_credit = sum(l["price"] for l in legs_costs if l["action"] == "SELL")
                        buy_debit = sum(l["price"] for l in legs_costs if l["action"] == "BUY")
                        scenario_net_credit = round(sell_credit - buy_debit, 2)
                        if scenario_net_credit <= 0:
                            self._record_rejection("NON_POSITIVE_SCENARIO_CREDIT")
                            continue

                        entry_costs = TransactionCostEngine.calculate_order_costs_breakdown(
                            legs_costs, lots=cand.lots, trade_date=trade_date
                        )
                        entry_brokerage = entry_costs["brokerage"]
                        entry_taxes = entry_costs["total"] - entry_brokerage
                        entry_total = entry_costs["total"]

                        # Calculate trade maximum risk (including round-trip costs)
                        wing_width = cand.strike_width
                        trade_max_loss_pts = max(0.0, wing_width - scenario_net_credit)
                        trade_max_loss_inr = round(trade_max_loss_pts * quantity, 2)
                        trade_risk_inr = getattr(cand, "total_risk_with_costs_inr", trade_max_loss_inr)
                        estimated_round_trip_costs = entry_total * 2.0
                        scenario_trade_risk_inr = round(trade_max_loss_inr + estimated_round_trip_costs, 2)
                        effective_trade_risk = max(trade_risk_inr, scenario_trade_risk_inr)

                        # MANDATORY CONDITION 3: Strictly enforce per-trade max risk <= INR 5,000
                        if effective_trade_risk > Config.get_max_risk_per_trade_inr():
                            self._record_rejection(f"RISK_CAP_BREACH(INR {effective_trade_risk:.0f} > 5000)")
                            continue

                        current_portfolio_risk = sum(t.max_risk_inr for t in open_trades)
                        projected_portfolio_risk = current_portfolio_risk + effective_trade_risk
                        if projected_portfolio_risk > Config.get_max_combined_portfolio_risk_inr():
                            self._record_rejection("PORTFOLIO_RISK_CAP_BREACH")
                            continue

                        trade_counter += 1
                        trade_id = f"BT-{scenario[:3]}-{trade_counter:04d}"

                        # Extract strikes
                        pe_legs = [l for l in cand.legs if l.option_type == "PE"]
                        ce_legs = [l for l in cand.legs if l.option_type == "CE"]
                        sp = [l.strike for l in pe_legs if l.action == "SELL"][0]
                        lp = [l.strike for l in pe_legs if l.action == "BUY"][0]
                        sc = [l.strike for l in ce_legs if l.action == "SELL"][0]
                        lc = [l.strike for l in ce_legs if l.action == "BUY"][0]

                        new_trade = BacktestTrade(
                            trade_id=trade_id,
                            strategy_id=cand.strategy_id,
                            strategy_name=cand.strategy_name,
                            engine_type=cand.engine_type,
                            scenario=scenario,
                            expiry=expiry,
                            entry_time=ts_str,
                            lots=cand.lots,
                            quantity=quantity,
                            short_strike=sp,
                            long_strike=lp,
                            short_strike_2=sc,
                            long_strike_2=lc,
                            net_credit_pts=scenario_net_credit,
                            entry_spread_cost=scenario_net_credit,
                            total_brokerage=entry_brokerage,
                            total_taxes=entry_taxes,
                            total_costs=entry_total,
                            max_risk_inr=effective_trade_risk,
                        )
                        open_trades.append(new_trade)
            except Exception as e:
                self._record_rejection(f"ANALYSIS_ERROR: {e}")

        # Settle any unclosed trades at last snapshot
        if open_trades and last_snap is not None:
            for ot in list(open_trades):
                self._close_trade_at_settlement(ot, last_snap, scenario, reason="EXPIRY_SETTLEMENT")
                current_equity += ot.net_pnl
                open_trades.remove(ot)
                self._record_trade(metrics[ot.strategy_id], ot)
                self._record_trade(metrics["COMBINED"], ot)

        return metrics


def build_scenario_metrics_summary(
    m: StrategyMetrics,
    scenario_id: str,
    scenario_name: str,
    category: str,
    wing_width: float,
    target_profit_pct: float,
    stop_loss_loss_mult: float,
    adx_filter_active: bool,
    adx_threshold: float,
    engine: ResearchBacktestEngine,
) -> ResearchScenarioMetrics:
    closed_trades = m.trades
    n_trades = len(closed_trades)
    wins = [t for t in closed_trades if t.net_pnl > 0]
    losses = [t for t in closed_trades if t.net_pnl <= 0]
    win_rate = round(len(wins) / n_trades * 100.0, 2) if n_trades > 0 else 0.0

    gross_pnl = round(sum(t.gross_pnl for t in closed_trades), 2)
    total_brokerage = round(sum(t.total_brokerage for t in closed_trades), 2)
    total_taxes = round(sum(t.total_taxes for t in closed_trades), 2)
    total_costs = round(sum(t.total_costs for t in closed_trades), 2)
    total_slippage = round(sum(t.slippage_cost for t in closed_trades), 2)
    net_pnl = round(sum(t.net_pnl for t in closed_trades), 2)
    roi_pct = round((net_pnl / STARTING_EQUITY) * 100.0, 2)

    gross_win = sum(t.gross_pnl for t in wins)
    gross_loss = abs(sum(t.gross_pnl for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    expectancy = round(net_pnl / n_trades, 2) if n_trades > 0 else 0.0

    largest_win = round(max((t.net_pnl for t in wins), default=0.0), 2)
    largest_loss = round(min((t.net_pnl for t in losses), default=0.0), 2)

    max_cons = 0
    cur_cons = 0
    for t in closed_trades:
        if t.net_pnl <= 0:
            cur_cons += 1
            if cur_cons > max_cons: max_cons = cur_cons
        else:
            cur_cons = 0

    # Chronological Split
    discovery_cutoff_date = "2026-03-24"
    disc_trades = [t for t in closed_trades if t.entry_time[:10] <= discovery_cutoff_date]
    test_trades = [t for t in closed_trades if t.entry_time[:10] > discovery_cutoff_date]

    disc_n = len(disc_trades)
    disc_wins = len([t for t in disc_trades if t.net_pnl > 0])
    disc_net = round(sum(t.net_pnl for t in disc_trades), 2)
    disc_wr = round(disc_wins / disc_n * 100.0, 2) if disc_n > 0 else 0.0
    disc_gw = sum(t.gross_pnl for t in disc_trades if t.net_pnl > 0)
    disc_gl = abs(sum(t.gross_pnl for t in disc_trades if t.net_pnl <= 0))
    disc_pf = round(disc_gw / disc_gl, 2) if disc_gl > 0 else (99.0 if disc_gw > 0 else 0.0)

    test_n = len(test_trades)
    test_wins = len([t for t in test_trades if t.net_pnl > 0])
    test_net = round(sum(t.net_pnl for t in test_trades), 2)
    test_wr = round(test_wins / test_n * 100.0, 2) if test_n > 0 else 0.0
    test_gw = sum(t.gross_pnl for t in test_trades if t.net_pnl > 0)
    test_gl = abs(sum(t.gross_pnl for t in test_trades if t.net_pnl <= 0))
    test_pf = round(test_gw / test_gl, 2) if test_gl > 0 else (99.0 if test_gw > 0 else 0.0)

    return ResearchScenarioMetrics(
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        category=category,
        wing_width=wing_width,
        target_profit_pct=target_profit_pct,
        stop_loss_loss_mult=stop_loss_loss_mult,
        stop_loss_debit_mult=1.0 + stop_loss_loss_mult,
        adx_filter_active=adx_filter_active,
        adx_threshold=adx_threshold,
        total_trades=n_trades,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate_pct=win_rate,
        gross_pnl=gross_pnl,
        total_brokerage=total_brokerage,
        total_taxes=total_taxes,
        total_costs=total_costs,
        total_slippage=total_slippage,
        net_pnl=net_pnl,
        roi_pct=roi_pct,
        profit_factor=profit_factor,
        expectancy_inr=expectancy,
        max_drawdown_inr=round(m.max_drawdown_inr, 2),
        max_drawdown_pct=round(m.max_drawdown_pct, 2),
        largest_win_inr=largest_win,
        largest_loss_inr=largest_loss,
        max_consecutive_losses=max_cons,
        discovery_trades=disc_n,
        discovery_net_pnl=disc_net,
        discovery_win_rate_pct=disc_wr,
        discovery_profit_factor=disc_pf,
        test_split_trades=test_n,
        test_split_net_pnl=test_net,
        test_split_win_rate_pct=test_wr,
        test_split_profit_factor=test_pf,
        evaluated_candidates=engine.evaluated_candidates,
        rejected_candidates=engine.rejected_candidates,
        rejection_reasons=engine.rejection_summary,
    )


def main():
    start_t = time.time()
    data_dir = os.path.join(WORKSPACE_ROOT, "instance", "historical_data")
    parquet_files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
    print(f"Loaded {len(parquet_files)} parquet sessions.")

    # 1. Cache causal daily spot records for ADX
    daily_records = []
    all_records = []
    print("Reconstructing chronological snapshots once for rapid execution...")
    for pf in parquet_files:
        try:
            df = pd.read_parquet(pf)
            valid, _, recs = HistoricalDataValidator.validate_dataframe(df, file_path=pf)
            if valid:
                all_records.extend(recs)
            if "spot_price" in df.columns and "timestamp" in df.columns:
                ts = str(df["timestamp"].iloc[0])[:10]
                daily_records.append({
                    "date": ts,
                    "open": float(df["spot_price"].iloc[0]),
                    "high": float(df["spot_price"].max()),
                    "low": float(df["spot_price"].min()),
                    "close": float(df["spot_price"].iloc[-1]),
                })
        except Exception as e:
            print(f"Error loading {pf}: {e}")

    df_daily = pd.DataFrame(daily_records).drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    cached_snapshots, _, _, _, _, _, _ = HistoricalDataValidator.reconstruct_snapshots(all_records)
    print(f"Reconstructed {len(cached_snapshots)} snapshots across {len(parquet_files)} sessions. Daily history: {len(df_daily)} days.")

    scenarios_to_run = [
        # --- PHASE 1: RECONCILED BASELINES ---
        {
            "id": "PHASE1_BASELINE_100W",
            "name": "Phase 1: Continuous Baseline (100-pt Wings, Auto-Select Default)",
            "cat": "BASELINE",
            "wing_width": 100.0,
            "target": 0.70,
            "stop": 0.50,
            "adx": False,
        },
        {
            "id": "PHASE1_BASELINE_50W",
            "name": "Phase 1: Continuous Baseline (50-pt Wings, Production Setting)",
            "cat": "BASELINE",
            "wing_width": 50.0,
            "target": 0.70,
            "stop": 0.50,
            "adx": False,
        },

        # --- PHASE 2: ADX EXPERIMENT (on both wing regimes) ---
        {
            "id": "PHASE2_ADX_ON_100W",
            "name": "Phase 2: 100-pt Wings + Causal ADX(14) < 22.0 Filter",
            "cat": "ADX_FILTER",
            "wing_width": 100.0,
            "target": 0.70,
            "stop": 0.50,
            "adx": True,
            "adx_thresh": 22.0,
        },
        {
            "id": "PHASE2_ADX_ON_50W",
            "name": "Phase 2: 50-pt Wings + Causal ADX(14) < 22.0 Filter",
            "cat": "ADX_FILTER",
            "wing_width": 50.0,
            "target": 0.70,
            "stop": 0.50,
            "adx": True,
            "adx_thresh": 22.0,
        },

        # --- PHASE 3: WING-WIDTH EXPERIMENT (50 vs 100) ---
        {
            "id": "PHASE3_WING_50PT",
            "name": "Phase 3: Wing Width = 50.0 pts (Standard Conservative)",
            "cat": "WING_WIDTH",
            "wing_width": 50.0,
            "target": 0.70,
            "stop": 0.50,
            "adx": False,
        },
        {
            "id": "PHASE3_WING_100PT",
            "name": "Phase 3: Wing Width = 100.0 pts (Strict <= INR 5,000 Risk Guard)",
            "cat": "WING_WIDTH",
            "wing_width": 100.0,
            "target": 0.70,
            "stop": 0.50,
            "adx": False,
        },

        # --- PHASE 4: TARGET SWEEPS (on 100-pt wing) ---
        {
            "id": "PHASE4_TARGET_50PCT",
            "name": "Phase 4: Target = 50% Credit Capture (Stop = 0.50x)",
            "cat": "TARGET_SWEEP",
            "wing_width": 100.0,
            "target": 0.50,
            "stop": 0.50,
            "adx": False,
        },
        {
            "id": "PHASE4_TARGET_60PCT",
            "name": "Phase 4: Target = 60% Credit Capture (Stop = 0.50x)",
            "cat": "TARGET_SWEEP",
            "wing_width": 100.0,
            "target": 0.60,
            "stop": 0.50,
            "adx": False,
        },
        {
            "id": "PHASE4_TARGET_70PCT",
            "name": "Phase 4: Target = 70% Credit Capture (Baseline Target)",
            "cat": "TARGET_SWEEP",
            "wing_width": 100.0,
            "target": 0.70,
            "stop": 0.50,
            "adx": False,
        },
        {
            "id": "PHASE4_TARGET_80PCT",
            "name": "Phase 4: Target = 80% Credit Capture (Stop = 0.50x)",
            "cat": "TARGET_SWEEP",
            "wing_width": 100.0,
            "target": 0.80,
            "stop": 0.50,
            "adx": False,
        },

        # --- PHASE 4: STOP-LOSS SWEEPS (on 100-pt wing) ---
        {
            "id": "PHASE4_STOP_050X",
            "name": "Phase 4: Stop = 0.50x Credit Loss / 1.50x Debit (Baseline Stop)",
            "cat": "STOP_SWEEP",
            "wing_width": 100.0,
            "target": 0.70,
            "stop": 0.50,
            "adx": False,
        },
        {
            "id": "PHASE4_STOP_100X",
            "name": "Phase 4: Stop = 1.00x Credit Loss / 2.00x Debit",
            "cat": "STOP_SWEEP",
            "wing_width": 100.0,
            "target": 0.70,
            "stop": 1.00,
            "adx": False,
        },
        {
            "id": "PHASE4_STOP_150X",
            "name": "Phase 4: Stop = 1.50x Credit Loss / 2.50x Debit",
            "cat": "STOP_SWEEP",
            "wing_width": 100.0,
            "target": 0.70,
            "stop": 1.50,
            "adx": False,
        },
    ]

    all_results = []
    print(f"\n==================== RUNNING RECONCILED RESEARCH MATRIX ({len(scenarios_to_run)} SCENARIOS) ====================")
    for sc in scenarios_to_run:
        s_id = sc["id"]
        s_name = sc["name"]
        print(f"Executing: [{s_id}] {s_name}...")
        
        eng = ResearchBacktestEngine(
            wing_width=sc["wing_width"],
            adx_filter_active=sc["adx"],
            adx_threshold=sc.get("adx_thresh", 22.0),
            daily_spot_df=df_daily,
            cached_snapshots=cached_snapshots,
            data_dir=data_dir,
            starting_equity=STARTING_EQUITY,
            target_profit_pct=sc["target"],
            stop_loss_credit_mult=sc["stop"],
        )
        
        sim_res = eng.run_simulation(parquet_files=parquet_files, scenarios=["BASE"])
        metrics_ic = sim_res["BASE"]["IRON_CONDOR"]
        
        scenario_metrics = build_scenario_metrics_summary(
            m=metrics_ic,
            scenario_id=s_id,
            scenario_name=s_name,
            category=sc["cat"],
            wing_width=sc["wing_width"],
            target_profit_pct=sc["target"],
            stop_loss_loss_mult=sc["stop"],
            adx_filter_active=sc["adx"],
            adx_threshold=sc.get("adx_thresh", 22.0),
            engine=eng,
        )
        
        all_results.append(scenario_metrics.to_dict())
        print(f"   -> Trades: {scenario_metrics.total_trades} | Net P&L: INR {scenario_metrics.net_pnl:,.2f} | Win Rate: {scenario_metrics.win_rate_pct}% | PF: {scenario_metrics.profit_factor} | Max DD: INR {scenario_metrics.max_drawdown_inr:,.2f}")

    elapsed = round(time.time() - start_t, 2)
    print(f"\nAll scenarios completed in {elapsed} seconds.")

    # Save artifact
    output_dir = os.path.join(WORKSPACE_ROOT, "instance", "research_reports")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "reconciled_research_matrix.json")
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "metadata": {
                    "execution_timestamp": datetime.now().isoformat(),
                    "total_historical_sessions": len(parquet_files),
                    "discovery_sessions": 75,
                    "discovery_period": "2024-10-24 to 2026-03-24",
                    "previously_examined_test_sessions": 30,
                    "previously_examined_test_period": "2026-03-30 to 2026-10-19",
                    "starting_equity_inr": STARTING_EQUITY,
                    "per_trade_risk_cap_inr": Config.get_max_risk_per_trade_inr(),
                    "consecutive_loss_cooldown_mins": Config.CONSECUTIVE_LOSS_COOLDOWN_MINS,
                    "annual_return_benchmark_pct": "8.0% - 10.0%",
                },
                "scenarios": all_results,
            },
            f,
            indent=2
        )
    print(f"Saved machine-readable research report to: {output_path}")


if __name__ == "__main__":
    main()
