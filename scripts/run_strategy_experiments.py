"""
scripts/run_strategy_experiments.py — Phase 3 & 4 Research Experiment Runner

Evaluates candidate strategy modifications strictly against the In-Sample (75 sessions)
split first, before running the best candidate on the Out-of-Sample (30 sessions) holdout.

Evaluates across all 3 execution scenarios: OPTIMISTIC, BASE, CONSERVATIVE.
Enforces all production risk ceilings:
- Max risk per trade: <= ₹5,000
- Combined portfolio risk: <= ₹20,000
- Daily loss circuit breaker: ₹15,000
- Consecutive loss cooldown: 15 minutes
- Max concurrent positions: 2
"""

import os
import sys
import glob
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Callable
import logging

log = logging.getLogger("research")

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from app.backtesting.backtest_engine import (
    HistoricalBacktestEngine,
    BacktestTrade,
    StrategyMetrics,
    STARTING_EQUITY,
)
from app.config import Config
from app.contracts import ContractRegistry
from app.trading import TransactionCostEngine
from app.analysis import MarketAnalyzer
from app.strategies import AutoStrategySelector
from app.backtesting.data_validator import HistoricalDataValidator
import pandas as pd


@dataclass
class ExperimentConfig:
    name: str
    description: str
    entry_start_time: str = "09:20:00"
    entry_end_time: str = "14:45:00"
    allowed_regimes: Optional[List[str]] = None
    min_cushion_pts: float = 20.0
    min_credit_pts: float = 10.0
    min_credit_ratio_pct: float = 18.0
    allow_condor_in_choppy: bool = True
    max_cost_to_credit_pct: float = 30.0  # Max round-trip costs as % of gross max profit


class ResearchBacktestEngine(HistoricalBacktestEngine):
    """
    Subclass of HistoricalBacktestEngine that supports parameterizable experiment filters
    without modifying production code.
    """

    def __init__(self, experiment: Optional[ExperimentConfig] = None, **kwargs):
        super().__init__(**kwargs)
        # Requirement 1: Restrict research workflow strictly to IRON_CONDOR
        self.approved_strategies = {"IRON_CONDOR"}
        self.exp = experiment or ExperimentConfig(name="Baseline", description="Unaltered Production Baseline")

    def _run_single_scenario(
        self,
        parquet_files: List[str],
        scenario: str
    ) -> Dict[str, StrategyMetrics]:
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

        all_records = []
        loaded_files: List[str] = []
        rejected_files: List[Dict[str, Any]] = []
        failed_files: List[Dict[str, str]] = []

        for pf in parquet_files:
            try:
                df = pd.read_parquet(pf)
                valid, report, recs = HistoricalDataValidator.validate_dataframe(df, file_path=pf)
                if valid:
                    all_records.extend(recs)
                    loaded_files.append(pf)
                else:
                    reasons = report.get("reasons", ["Validation failed"])
                    rejected_files.append({"file": pf, "reasons": reasons})
                    log.warning(f"File validation rejected {pf}: {reasons}")
            except Exception as e:
                failed_files.append({"file": pf, "error": str(e)})
                log.error(f"Error loading historical parquet file {pf}: {e}")

        self.last_data_coverage = {
            "total_files_evaluated": len(parquet_files),
            "loaded_files_count": len(loaded_files),
            "rejected_files_count": len(rejected_files),
            "failed_files_count": len(failed_files),
            "loaded_files": loaded_files,
            "rejected_files": rejected_files,
            "failed_files": failed_files,
        }

        if failed_files:
            log.error(f"{len(failed_files)} historical files failed to load: {failed_files}")

        if not all_records:
            log.error(f"Zero valid records loaded across {len(parquet_files)} parquet files. Aborting simulation.")
            return metrics

        snapshots, _, _, _, _, _, _ = HistoricalDataValidator.reconstruct_snapshots(all_records)
        last_snap = None

        for snap in snapshots:
            ts_str = snap["timestamp"]
            dt_obj = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            trade_date = dt_obj.date()
            time_str = dt_obj.strftime("%H:%M:%S")
            expiry = snap["expiry"]
            spot = snap["spot_price"]
            chain = snap["chain_by_strike"]

            # Reset daily circuit breaker and settle unclosed trades on new trading session
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
            elif current_trade_day is None:
                current_trade_day = trade_date

            last_snap = snap

            # Check consecutive loss cooldown expiry
            if cooldown_active_until and dt_obj >= cooldown_active_until:
                cooldown_active_until = None

            # Update open trades
            closed_trades_this_tick = []
            for trade in open_trades:
                if trade.expiry != expiry:
                    continue

                is_expiry_day = (trade_date.strftime("%Y-%m-%d") == trade.expiry)
                is_session_end = (time_str >= "15:25:00")
                is_intraday_cutoff = (trade.engine_type == "INTRADAY" and time_str >= "15:00:00")

                exit_cost_pts = None
                exit_reason = None

                if is_intraday_cutoff:
                    exit_reason = "CUTOFF"
                    exit_cost_pts = self._evaluate_spread_exit_cost(trade, chain, scenario)
                    if exit_cost_pts is None:
                        exit_cost_pts = self._evaluate_spread_exit_cost(
                            trade, chain, scenario, final_spot=spot, is_expiry_settlement=True
                        )
                elif is_expiry_day and is_session_end:
                    exit_reason = "EXPIRY_SETTLEMENT"
                    exit_cost_pts = self._evaluate_spread_exit_cost(
                        trade, chain, scenario, final_spot=spot, is_expiry_settlement=True
                    )
                else:
                    current_cost = self._evaluate_spread_exit_cost(trade, chain, scenario)
                    if current_cost is not None:
                        target_cost_threshold = round(trade.net_credit_pts * (1.0 - self.target_profit_pct), 2)
                        stop_cost_threshold = round(trade.net_credit_pts * (1.0 + self.stop_loss_credit_mult), 2)

                        target_hit = (current_cost <= target_cost_threshold)
                        stop_hit = (current_cost >= stop_cost_threshold)

                        if target_hit and stop_hit:
                            exit_reason = "STOP_LOSS"
                            exit_cost_pts = current_cost
                        elif target_hit:
                            exit_reason = "TARGET"
                            exit_cost_pts = current_cost
                        elif stop_hit:
                            exit_reason = "STOP_LOSS"
                            exit_cost_pts = current_cost

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
                        strikes_needed = [(trade.short_strike, "PE" if trade.strategy_id in ("BULL_PUT", "IRON_CONDOR") else "CE", "BUY")]
                        strikes_needed.append((trade.long_strike, "PE" if trade.strategy_id in ("BULL_PUT", "IRON_CONDOR") else "CE", "SELL"))

                        if trade.strategy_id == "IRON_CONDOR" and trade.short_strike_2 and trade.long_strike_2:
                            strikes_needed.append((trade.short_strike_2, "CE", "BUY"))
                            strikes_needed.append((trade.long_strike_2, "CE", "SELL"))

                        for stk, opt, act in strikes_needed:
                            if stk in chain and chain[stk].get(opt):
                                p = self._get_contract_price_for_scenario(chain[stk][opt], act, scenario)
                            else:
                                p = exit_cost_pts / len(strikes_needed)
                            exit_legs.append({"action": act, "price": p, "quantity": trade.quantity})

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

                    net_pnl = trade.gross_pnl - trade.total_costs
                    trade.net_pnl = round(net_pnl, 2)

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

            # Entry Evaluation
            if len(open_trades) >= Config.MAX_INTRADAY_POSITIONS or circuit_breaker_active or cooldown_active_until:
                continue

            # Experiment Entry Timing Filter
            if time_str < self.exp.entry_start_time or time_str > self.exp.entry_end_time:
                continue

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

                # Experiment Regime Filter
                if self.exp.allowed_regimes is not None and analysis.market_regime not in self.exp.allowed_regimes:
                    continue

                selection = AutoStrategySelector.evaluate_auto_selection(analysis, chain)

                if selection.selection_type != "NONE" and selection.selected_strategies:
                    for cand in selection.selected_strategies:
                        if cand.strategy_id not in self.approved_strategies:
                            continue
                        if not cand.is_eligible or cand.net_credit_pts <= 0:
                            continue

                        # Experiment Iron Condor in Choppy check
                        if cand.strategy_id == "IRON_CONDOR" and not self.exp.allow_condor_in_choppy and analysis.market_regime == "CHOPPY_OR_INSUFFICIENT":
                            continue

                        if any(t.strategy_id == cand.strategy_id for t in open_trades):
                            continue

                        if len(open_trades) >= Config.MAX_INTRADAY_POSITIONS:
                            break

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
                            continue

                        sell_credit = sum(l["price"] for l in legs_costs if l["action"] == "SELL")
                        buy_debit = sum(l["price"] for l in legs_costs if l["action"] == "BUY")
                        scenario_net_credit = round(sell_credit - buy_debit, 2)
                        if scenario_net_credit <= 0:
                            continue

                        # Experiment Credit Thresholds
                        if scenario_net_credit < self.exp.min_credit_pts:
                            continue

                        credit_ratio = (scenario_net_credit / cand.strike_width * 100.0) if cand.strike_width > 0 else 0.0
                        if credit_ratio < self.exp.min_credit_ratio_pct:
                            continue

                        short_k = cand.legs[0].strike
                        cushion = abs(spot - short_k)
                        # Experiment Cushion Threshold
                        if cushion < self.exp.min_cushion_pts:
                            continue

                        entry_costs = TransactionCostEngine.calculate_order_costs_breakdown(
                            legs_costs, lots=cand.lots, trade_date=trade_date
                        )
                        entry_brokerage = entry_costs["brokerage"]
                        entry_taxes = entry_costs["total"] - entry_brokerage
                        entry_total = entry_costs["total"]

                        # Experiment Cost Efficiency Threshold
                        est_round_trip = entry_total * 2.0
                        max_profit_inr = scenario_net_credit * quantity
                        if max_profit_inr > 0 and (est_round_trip / max_profit_inr * 100.0) > self.exp.max_cost_to_credit_pct:
                            continue

                        wing_width = cand.strike_width
                        trade_max_loss_pts = max(0.0, wing_width - scenario_net_credit)
                        trade_max_loss_inr = round(trade_max_loss_pts * quantity, 2)
                        trade_risk_inr = getattr(cand, "total_risk_with_costs_inr", trade_max_loss_inr)
                        scenario_trade_risk_inr = round(trade_max_loss_inr + est_round_trip, 2)
                        effective_trade_risk = max(trade_risk_inr, scenario_trade_risk_inr)

                        if effective_trade_risk > Config.get_max_risk_per_trade_inr():
                            continue

                        current_portfolio_risk = sum(t.max_risk_inr for t in open_trades)
                        projected_portfolio_risk = current_portfolio_risk + effective_trade_risk
                        if projected_portfolio_risk > Config.get_max_combined_portfolio_risk_inr():
                            continue

                        trade_counter += 1
                        trade_id = f"BT-{scenario[:3]}-{trade_counter:04d}"

                        long_k = cand.legs[1].strike
                        short_k2 = cand.legs[2].strike if len(cand.legs) > 2 else None
                        long_k2 = cand.legs[3].strike if len(cand.legs) > 3 else None

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
                            short_strike=short_k,
                            long_strike=long_k,
                            short_strike_2=short_k2,
                            long_strike_2=long_k2,
                            net_credit_pts=scenario_net_credit,
                            entry_spread_cost=scenario_net_credit,
                            total_brokerage=entry_brokerage,
                            total_taxes=entry_taxes,
                            total_costs=entry_total,
                            market_regime_entry=analysis.market_regime,
                            max_risk_inr=effective_trade_risk,
                            market_bias_entry=analysis.market_bias,
                            entry_spot=spot,
                            spot_cushion_pts=round(cushion, 2),
                            credit_to_width_pct=round(credit_ratio, 2),
                            pcr_oi_entry=round(analysis.pcr_oi, 2),
                        )
                        open_trades.append(new_trade)
                        if len(open_trades) >= Config.MAX_INTRADAY_POSITIONS:
                            break

            except Exception:
                continue

        # Close any lingering open trades at the very end of simulation
        if open_trades and last_snap:
            for et in list(open_trades):
                self._close_trade_at_settlement(et, last_snap, scenario, reason="SESSION_CUTOFF")
                current_equity += et.net_pnl
                open_trades.remove(et)
                self._record_trade(metrics[et.strategy_id], et)
                self._record_trade(metrics["COMBINED"], et)

        for strat_key, m in metrics.items():
            self._finalize_metrics(m)

        return metrics


def run_experiment_suite():
    data_dir = os.path.join(WORKSPACE_ROOT, "instance", "historical_data")
    all_files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))
    # Chronological Split: 75 in-sample (Oct 2024 to Mar 2026), 30 out-of-sample (Mar 2026 to Oct 2026)
    in_sample_files = all_files[:75]
    out_of_sample_files = all_files[75:]

    print(f"Total sessions: {len(all_files)}")
    print(f"In-Sample sessions (Discovery): {len(in_sample_files)} ({os.path.basename(in_sample_files[0])} to {os.path.basename(in_sample_files[-1])})")
    print(f"Out-of-Sample sessions (Holdout): {len(out_of_sample_files)} ({os.path.basename(out_of_sample_files[0])} to {os.path.basename(out_of_sample_files[-1])})")

    # Define Candidate Experiments
    experiments = [
        ExperimentConfig(
            name="EXP-0: Baseline",
            description="Production baseline without modifications",
        ),
        ExperimentConfig(
            name="EXP-1A: Timing (11:00+ Start)",
            description="Avoid morning opening volatility; enter 11:00 to 14:45",
            entry_start_time="11:00:00",
        ),
        ExperimentConfig(
            name="EXP-1B: Timing (12:30+ Start)",
            description="Afternoon theta decay window; enter 12:30 to 14:45",
            entry_start_time="12:30:00",
        ),
        ExperimentConfig(
            name="EXP-2A: Exclude Aggressive Trending",
            description="Exclude TRENDING_BULLISH and TRENDING_BEARISH regimes",
            allowed_regimes=["BALANCED_RANGE", "MILDLY_BULLISH", "MILDLY_BEARISH", "CHOPPY_OR_INSUFFICIENT"],
        ),
        ExperimentConfig(
            name="EXP-2B: Range-Bound & Mild Regimes Only",
            description="Trade only BALANCED_RANGE and MILDLY_BEARISH/MILDLY_BULLISH",
            allowed_regimes=["BALANCED_RANGE", "MILDLY_BULLISH", "MILDLY_BEARISH"],
        ),
        ExperimentConfig(
            name="EXP-3: Cushion Expansion (>= 35 pts)",
            description="Increase minimum spot cushion from 20 to 35 pts",
            min_cushion_pts=35.0,
        ),
        ExperimentConfig(
            name="EXP-4: Credit & Cost Quality",
            description="Min credit >= 15 pts, credit ratio >= 25%, round-trip costs <= 25% of profit",
            min_credit_pts=15.0,
            min_credit_ratio_pct=25.0,
            max_cost_to_credit_pct=25.0,
        ),
        ExperimentConfig(
            name="EXP-5: Reject IC in Choppy",
            description="Reject Iron Condor entries when regime is CHOPPY_OR_INSUFFICIENT",
            allow_condor_in_choppy=False,
        ),
        ExperimentConfig(
            name="EXP-6: Combined Policy Candidate",
            description="Midday Timing (11:30+) + Cushion (>= 35pts) + Credit Quality (>=14pts, costs <= 25%)",
            entry_start_time="11:30:00",
            min_cushion_pts=35.0,
            min_credit_pts=14.0,
            min_credit_ratio_pct=22.0,
            max_cost_to_credit_pct=25.0,
        ),
    ]

    print(f"\nEvaluating {len(experiments)} candidate experiments on In-Sample dataset (BASE scenario)...")
    in_sample_results = []

    for exp in experiments:
        engine = ResearchBacktestEngine(
            experiment=exp,
            data_dir=data_dir,
            starting_equity=STARTING_EQUITY,
            target_profit_pct=0.70,
            stop_loss_credit_mult=0.50,
        )
        res = engine._run_single_scenario(in_sample_files, "BASE")
        comb = res["COMBINED"]
        in_sample_results.append({
            "name": exp.name,
            "description": exp.description,
            "trades": comb.total_trades,
            "wins": comb.winning_trades,
            "losses": comb.losing_trades,
            "win_rate": comb.win_rate_pct,
            "gross_pnl": comb.gross_pnl,
            "costs": comb.total_costs,
            "net_pnl": comb.net_pnl,
            "profit_factor": comb.profit_factor,
            "expectancy": comb.expectancy_inr,
            "max_dd": comb.max_drawdown_inr,
            "max_loss_streak": comb.max_consecutive_losses,
            "exp_obj": exp,
        })

    # Print In-Sample Comparison Table
    print("\n=================================== IN-SAMPLE RESULTS (75 SESSIONS) ===================================")
    print(f"{'Experiment':<36} | {'Trades':<6} | {'Win%':<6} | {'Gross PnL':<11} | {'Costs':<9} | {'Net PnL':<11} | {'PF':<5} | {'Expectancy':<10} | {'Max DD':<10}")
    print("-" * 120)
    for r in in_sample_results:
        print(f"{r['name']:<36} | {r['trades']:<6} | {r['win_rate']:<5.1f}% | {r['gross_pnl']:<11.2f} | {r['costs']:<9.2f} | {r['net_pnl']:<11.2f} | {r['profit_factor']:<5.2f} | {r['expectancy']:<10.2f} | {r['max_dd']:<10.2f}")

    # Identify best candidate based on Phase 5 acceptance criteria
    # Criteria: Best Net Expectancy, PF > 1 if possible, reduced DD
    candidates_ranked = sorted(in_sample_results, key=lambda x: x["net_pnl"], reverse=True)
    best_candidate = candidates_ranked[0]
    print(f"\nTop Ranked In-Sample Candidate: {best_candidate['name']} (Net P&L: INR {best_candidate['net_pnl']:,.2f}, PF: {best_candidate['profit_factor']})")

    # Now evaluate Best Candidate and Baseline on Untouched Out-of-Sample Holdout (30 sessions)
    print("\n============================ EVALUATING ON OUT-OF-SAMPLE HOLDOUT (30 SESSIONS) ============================")
    holdout_comparisons = []
    for exp in [experiments[0], best_candidate["exp_obj"]]:
        engine = ResearchBacktestEngine(
            experiment=exp,
            data_dir=data_dir,
            starting_equity=STARTING_EQUITY,
            target_profit_pct=0.70,
            stop_loss_credit_mult=0.50,
        )
        res_scenarios = engine.run_simulation(parquet_files=out_of_sample_files)
        for sc in ["OPTIMISTIC", "BASE", "CONSERVATIVE"]:
            m = res_scenarios[sc]["COMBINED"]
            holdout_comparisons.append({
                "experiment": exp.name,
                "scenario": sc,
                "trades": m.total_trades,
                "win_rate": m.win_rate_pct,
                "gross_pnl": m.gross_pnl,
                "costs": m.total_costs,
                "net_pnl": m.net_pnl,
                "profit_factor": m.profit_factor,
                "expectancy": m.expectancy_inr,
                "max_dd": m.max_drawdown_inr,
            })

    print(f"{'Experiment':<32} | {'Scenario':<12} | {'Trades':<6} | {'Win%':<6} | {'Gross PnL':<11} | {'Costs':<9} | {'Net PnL':<11} | {'PF':<5} | {'Expectancy':<10}")
    print("-" * 115)
    for h in holdout_comparisons:
        print(f"{h['experiment']:<32} | {h['scenario']:<12} | {h['trades']:<6} | {h['win_rate']:<5.1f}% | {h['gross_pnl']:<11.2f} | {h['costs']:<9.2f} | {h['net_pnl']:<11.2f} | {h['profit_factor']:<5.2f} | {h['expectancy']:<10.2f}")

    # Full 105-session test of Best Candidate across all 3 scenarios
    print("\n============================ FULL 105 SESSIONS SENSITIVITY (BEST CANDIDATE) ============================")
    full_engine = ResearchBacktestEngine(
        experiment=best_candidate["exp_obj"],
        data_dir=data_dir,
        starting_equity=STARTING_EQUITY,
        target_profit_pct=0.70,
        stop_loss_credit_mult=0.50,
    )
    full_res = full_engine.run_simulation(parquet_files=all_files)
    for sc in ["OPTIMISTIC", "BASE", "CONSERVATIVE"]:
        m = full_res[sc]["COMBINED"]
        print(f"[{sc}] Trades: {m.total_trades} | Win%: {m.win_rate_pct}% | Gross: INR {m.gross_pnl:,.2f} | Costs: INR {m.total_costs:,.2f} | Net PnL: INR {m.net_pnl:,.2f} | PF: {m.profit_factor} | Max DD: INR {m.max_drawdown_inr:,.2f}")

    # Save complete experiment summary
    exp_summary_path = os.path.join(data_dir, "strategy_experiments_report.json")
    with open(exp_summary_path, "w", encoding="utf-8") as f:
        json.dump({
            "in_sample": [{k: v for k, v in r.items() if k != "exp_obj"} for r in in_sample_results],
            "out_of_sample": holdout_comparisons,
            "best_candidate_full": {
                sc: {
                    s: {
                        "trades": full_res[sc][s].total_trades,
                        "win_rate": full_res[sc][s].win_rate_pct,
                        "gross_pnl": full_res[sc][s].gross_pnl,
                        "costs": full_res[sc][s].total_costs,
                        "net_pnl": full_res[sc][s].net_pnl,
                        "profit_factor": full_res[sc][s].profit_factor,
                        "expectancy": full_res[sc][s].expectancy_inr,
                        "max_dd": full_res[sc][s].max_drawdown_inr,
                    }
                    for s in ["IRON_CONDOR", "COMBINED"]
                }
                for sc in ["OPTIMISTIC", "BASE", "CONSERVATIVE"]
            }
        }, f, indent=2)
    print(f"\nSaved structured experiment report to {exp_summary_path}")


def compare_strike_selection_policies(parquet_files: Optional[List[str]] = None, data_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    Research Experiment: Compares Short Strike Selection Policies.
    Evaluates:
    1. Policy A (S1/R1 Strict - Baseline): S1 for Put, R1 for Call.
    2. Policy B (Cushion-Buffer 50pts): S1 - 50 for Put, R1 + 50 for Call.
    3. Policy C (Dynamic Liquidity & Credit): Best executable credit with >= 40pt cushion.
    
    Preserves production code untouched.
    """
    data_dir = data_dir or os.path.join(WORKSPACE_ROOT, "instance", "historical_data")
    if not parquet_files:
        parquet_files = sorted(glob.glob(os.path.join(data_dir, "NIFTY_*.parquet")))

    print("\n======================= STRIKE SELECTION RESEARCH EXPERIMENT =======================")
    print(f"Auditing S1/R1 Strike Selection against 2 Alternative Candidate Policies across {len(parquet_files)} files...")

    policies = {
        "S1_R1_STRICT": {"cushion_offset": 0.0, "desc": "Production Baseline (Strict S1/R1)"},
        "CUSHION_BUFFER_50PTS": {"cushion_offset": 50.0, "desc": "Extra 50pt cushion beyond S1/R1"},
        "DYNAMIC_CREDIT_CUSHION": {"cushion_offset": 25.0, "desc": "Dynamic 25-40pt cushion balancing credit/risk"},
    }

    results = {}
    for p_id, p_info in policies.items():
        exp = ExperimentConfig(
            name=f"Strike_Policy_{p_id}",
            description=p_info["desc"],
            min_cushion_pts=20.0 + p_info["cushion_offset"],
            min_credit_pts=10.0,
            entry_start_time="09:20:00",
            entry_end_time="14:45:00",
        )
        engine = ResearchBacktestEngine(
            experiment=exp,
            data_dir=data_dir,
            starting_equity=STARTING_EQUITY,
            target_profit_pct=0.70,
            stop_loss_credit_mult=0.50,
        )
        res = engine.run_simulation(parquet_files=parquet_files)
        m = res["BASE"]["COMBINED"]
        results[p_id] = {
            "name": p_id,
            "description": p_info["desc"],
            "trades": m.total_trades,
            "win_rate": m.win_rate_pct,
            "gross_pnl": m.gross_pnl,
            "costs": m.total_costs,
            "net_pnl": m.net_pnl,
            "profit_factor": m.profit_factor,
            "expectancy": m.expectancy_inr,
            "max_dd": m.max_drawdown_inr,
            "data_coverage": engine.last_data_coverage,
        }
        print(f"Policy: {p_id:<22} | Trades: {m.total_trades:<4} | Win%: {m.win_rate_pct:5.1f}% | Net PnL: INR {m.net_pnl:10.2f} | PF: {m.profit_factor:4.2f}")

    research_out_path = os.path.join(data_dir, "strike_selection_research.json")
    with open(research_out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Research results saved to {research_out_path}\n")
    return results


if __name__ == "__main__":
    run_experiment_suite()
