"""
app/backtesting/backtest_engine.py — High-Fidelity Historical Options Backtest Engine.

Simulates the three approved defined-risk credit spread strategies:
1. BULL_PUT (Bull Put Credit Spread)
2. BEAR_CALL (Bear Call Credit Spread)
3. IRON_CONDOR (Iron Condor)

Key Safeguards & Architecture:
- Point-in-time chronological replay without look-ahead bias.
- Pure evaluation using production MarketAnalyzer, AutoStrategySelector, and StrategyBuilder.
- Official ContractRegistry lot sizes honoring all historical circular transition dates.
- Realistic statutory transaction cost calculations via TransactionCostCalculator.
- Three independent execution scenarios (OPTIMISTIC, BASE, CONSERVATIVE) with explicit ESTIMATED_SPREAD labels.
- Strict portfolio risk management matching authoritative production rules:
  - Starting Equity: ₹10,00,000
  - Max risk per trade cap: ₹5,000 (0.5% equity, Config.get_max_risk_per_trade_inr())
  - Combined portfolio risk cap: ₹20,000 (2.0% equity, Config.get_max_combined_portfolio_risk_inr())
  - Daily loss circuit breaker: ₹15,000 (1.5% equity, Config.get_max_daily_loss_inr()) halts new trades
  - Consecutive loss cooldown: 1 loss exit initiates 15-minute cooldown (Config.CONSECUTIVE_LOSS_COOLDOWN_MINS)
  - Profit target: buyback cost reaches 30% of credit (70% credit profit capture)
  - Stop loss: buyback cost reaches 150% of credit (loss of 50% of credit)
  - Maximum concurrent positions: 2 defined-risk spreads (Config.MAX_INTRADAY_POSITIONS)
  - Dataset coverage: Expiry-Day / 0DTE backtest over available expiry sessions
  - Intraday positions closed at 15:00:00 cutoff; positional held to target/SL or expiry settlement.
"""

import os
import glob
import math
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple, Set
import pandas as pd

from app.config import Config
from app.analysis import MarketAnalyzer, MarketAnalysisResult
from app.strategies import (
    StrategyBuilder,
    AutoStrategySelector,
    SpreadCandidate,
    OptionLegSpec,
)
from app.trading import TransactionCostEngine
from app.contracts import ContractRegistry
from app.backtesting.data_validator import HistoricalDataValidator

log = logging.getLogger("backtester.engine")
IST = ZoneInfo("Asia/Kolkata")
STARTING_EQUITY = 1_000_000.0  # ₹10,00,000 starting equity


@dataclass
class BacktestTrade:
    """Individual simulated trade record."""
    trade_id: str
    strategy_id: str          # BULL_PUT, BEAR_CALL, IRON_CONDOR
    strategy_name: str
    engine_type: str          # INTRADAY, POSITIONAL
    scenario: str             # OPTIMISTIC, BASE, CONSERVATIVE
    expiry: str
    entry_time: str
    exit_time: Optional[str] = None
    lots: int = 1
    quantity: int = 50
    short_strike: float = 0.0
    long_strike: float = 0.0
    short_strike_2: Optional[float] = None
    long_strike_2: Optional[float] = None
    net_credit_pts: float = 0.0
    entry_spread_cost: float = 0.0
    exit_spread_cost: float = 0.0
    exit_reason: str = "OPEN"  # TARGET, STOP_LOSS, CUTOFF, EXPIRY_SETTLEMENT
    gross_pnl: float = 0.0
    total_brokerage: float = 0.0
    total_taxes: float = 0.0
    total_costs: float = 0.0
    slippage_cost: float = 0.0
    net_pnl: float = 0.0
    fill_tier: str = "ESTIMATED_SPREAD"
    market_regime_entry: str = "BALANCED_RANGE"
    max_risk_inr: float = 0.0
    market_bias_entry: str = "NEUTRAL"
    entry_spot: float = 0.0
    spot_cushion_pts: float = 0.0
    credit_to_width_pct: float = 0.0
    pcr_oi_entry: float = 1.0
    holding_time_mins: float = 0.0


@dataclass
class StrategyMetrics:
    """Performance metrics for an individual strategy or portfolio scenario."""
    strategy_id: str
    scenario: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate_pct: float = 0.0
    gross_pnl: float = 0.0
    total_brokerage: float = 0.0
    total_taxes: float = 0.0
    total_costs: float = 0.0
    total_slippage: float = 0.0
    net_pnl: float = 0.0
    roi_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy_inr: float = 0.0
    max_drawdown_inr: float = 0.0
    max_drawdown_pct: float = 0.0
    largest_win_inr: float = 0.0
    largest_loss_inr: float = 0.0
    max_consecutive_losses: int = 0
    monthly_pnl: Dict[str, float] = field(default_factory=dict)
    pnl_by_regime: Dict[str, float] = field(default_factory=dict)
    trades: List[BacktestTrade] = field(default_factory=list)


class HistoricalBacktestEngine:
    """
    Executes multi-scenario, point-in-time replay over historical Parquet datasets.
    """

    def __init__(
        self,
        data_dir: Optional[str] = None,
        starting_equity: float = STARTING_EQUITY,
        target_profit_pct: float = 0.70,   # Capture 70% of credit (buyback cost reaches 30% of credit)
        stop_loss_credit_mult: float = 0.50, # Exit if loss equals 50% of credit (buyback cost reaches 150% of credit)
    ):
        self.data_dir = data_dir or os.path.join(Config.INSTANCE_DIR, "historical_data")
        self.starting_equity = starting_equity
        self.target_profit_pct = target_profit_pct
        self.stop_loss_credit_mult = stop_loss_credit_mult
        self.approved_strategies = {"BULL_PUT", "BEAR_CALL", "IRON_CONDOR"}

    def _get_contract_price_for_scenario(
        self,
        quote_dict: Dict[str, Any],
        action: str,
        scenario: str
    ) -> float:
        """
        Extracts appropriate fill price for BUY or SELL based on execution scenario.
        Always enforces NSE tick size (0.05 INR) and floor.
        """
        ltp = float(quote_dict.get("ltp", 0.0) or 0.0)
        if ltp <= 0.05:
            return 0.05

        action_u = action.upper()
        if scenario == "OPTIMISTIC":
            bid = float(quote_dict.get("opt_bid", quote_dict.get("bid", ltp * 0.9995)))
            ask = float(quote_dict.get("opt_ask", quote_dict.get("ask", ltp * 1.0005)))
        elif scenario == "CONSERVATIVE":
            bid = float(quote_dict.get("cons_bid", quote_dict.get("bid", ltp * 0.9950)))
            ask = float(quote_dict.get("cons_ask", quote_dict.get("ask", ltp * 1.0050)))
        else:  # BASE
            bid = float(quote_dict.get("base_bid", quote_dict.get("bid", ltp * 0.9975)))
            ask = float(quote_dict.get("base_ask", quote_dict.get("ask", ltp * 1.0025)))

        return max(0.05, bid if action_u == "SELL" else ask)

    def _evaluate_spread_exit_cost(
        self,
        trade: BacktestTrade,
        chain: Dict[float, Dict[str, Any]],
        scenario: str,
        final_spot: Optional[float] = None,
        is_expiry_settlement: bool = False
    ) -> Optional[float]:
        """
        Calculates the cost to close (buy back shorts, sell long wings).
        Returns cost in points per share, or None if quotes are missing.
        """
        if is_expiry_settlement and final_spot is not None:
            # Expiry settlement: compute intrinsic values
            closing_cost_pts = 0.0
            if trade.strategy_id == "BULL_PUT":
                # Short Put intrinsic: max(0, short_strike - spot)
                short_val = max(0.0, trade.short_strike - final_spot)
                long_val = max(0.0, trade.long_strike - final_spot)
                closing_cost_pts = short_val - long_val
            elif trade.strategy_id == "BEAR_CALL":
                short_val = max(0.0, final_spot - trade.short_strike)
                long_val = max(0.0, final_spot - trade.long_strike)
                closing_cost_pts = short_val - long_val
            elif trade.strategy_id == "IRON_CONDOR":
                # Put wing: short_strike (put), long_strike (put)
                put_short = max(0.0, trade.short_strike - final_spot)
                put_long = max(0.0, trade.long_strike - final_spot)
                # Call wing: short_strike_2 (call), long_strike_2 (call)
                call_short = max(0.0, final_spot - (trade.short_strike_2 or 0.0))
                call_long = max(0.0, final_spot - (trade.long_strike_2 or 0.0))
                closing_cost_pts = (put_short - put_long) + (call_short - call_long)
            return max(0.0, closing_cost_pts)

        # Standard market valuation
        closing_cost_pts = 0.0
        strikes_needed = [(trade.short_strike, "PE" if trade.strategy_id in ("BULL_PUT", "IRON_CONDOR") else "CE", "BUY")]
        strikes_needed.append((trade.long_strike, "PE" if trade.strategy_id in ("BULL_PUT", "IRON_CONDOR") else "CE", "SELL"))

        if trade.strategy_id == "IRON_CONDOR" and trade.short_strike_2 and trade.long_strike_2:
            strikes_needed.append((trade.short_strike_2, "CE", "BUY"))
            strikes_needed.append((trade.long_strike_2, "CE", "SELL"))

        for stk, opt, act in strikes_needed:
            if stk not in chain or not chain[stk].get(opt):
                return None  # Missing quote
            q = chain[stk][opt]
            p = self._get_contract_price_for_scenario(q, act, scenario)
            if act == "BUY":
                closing_cost_pts += p
            else:
                closing_cost_pts -= p

        return closing_cost_pts

    def _close_trade_at_settlement(
        self,
        trade: BacktestTrade,
        snap: Dict[str, Any],
        scenario: str,
        reason: str = "SESSION_CUTOFF"
    ) -> None:
        """
        Settles an open intraday/0DTE position when the session ends or transitions
        to a new date/expiry, preventing ghost trades from lingering across sessions.
        """
        chain = snap["chain_by_strike"]
        spot = snap["spot_price"]
        ts_str = snap["timestamp"]
        exit_cost_pts = self._evaluate_spread_exit_cost(
            trade, chain, scenario, final_spot=spot, is_expiry_settlement=True
        )
        if exit_cost_pts is None:
            exit_cost_pts = trade.entry_spread_cost

        trade.exit_time = ts_str
        trade.exit_reason = reason
        trade.exit_spread_cost = exit_cost_pts
        trade.gross_pnl = round((trade.net_credit_pts - exit_cost_pts) * trade.quantity, 2)
        trade.total_brokerage = 0.0
        trade.total_taxes = 0.0
        trade.total_costs = round(trade.total_brokerage + trade.total_taxes, 2)
        num_legs = 4 if trade.strategy_id == "IRON_CONDOR" else 2
        slippage_rate = 0.00 if scenario == "OPTIMISTIC" else 0.05 if scenario == "BASE" else 0.10
        trade.slippage_cost = round(slippage_rate * num_legs * trade.quantity, 2)
        trade.net_pnl = round(trade.gross_pnl - trade.total_costs, 2)
        try:
            t_in = datetime.strptime(trade.entry_time, "%Y-%m-%d %H:%M:%S")
            t_out = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            trade.holding_time_mins = round((t_out - t_in).total_seconds() / 60.0, 1)
        except Exception:
            trade.holding_time_mins = 0.0

    def run_simulation(
        self,
        parquet_files: Optional[List[str]] = None,
        scenarios: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, StrategyMetrics]]:
        """
        Runs the full chronological backtest across all available Parquet datasets.
        Returns: {scenario: {strategy_id: StrategyMetrics, 'COMBINED': StrategyMetrics}}.
        """
        if not parquet_files:
            pattern = os.path.join(self.data_dir, "NIFTY_*.parquet")
            parquet_files = sorted(glob.glob(pattern))

        if not parquet_files:
            log.warning("No historical Parquet files found for simulation.")
            return {}

        active_scenarios = scenarios or ["OPTIMISTIC", "BASE", "CONSERVATIVE"]
        results = {}

        for scenario in active_scenarios:
            log.info(f"Executing backtest simulation for scenario: {scenario}...")
            scenario_results = self._run_single_scenario(parquet_files, scenario)
            results[scenario] = scenario_results

        return results

    def _run_single_scenario(
        self,
        parquet_files: List[str],
        scenario: str
    ) -> Dict[str, StrategyMetrics]:
        """
        Executes a single pass over chronological historical datasets for one scenario.
        """
        metrics: Dict[str, StrategyMetrics] = {
            s: StrategyMetrics(strategy_id=s, scenario=scenario)
            for s in ["BULL_PUT", "BEAR_CALL", "IRON_CONDOR", "COMBINED"]
        }

        # Portfolio State
        current_equity = self.starting_equity
        open_trades: List[BacktestTrade] = []
        consecutive_losses = 0
        cooldown_active_until = None
        current_trade_day = None
        daily_loss_incurred = 0.0
        circuit_breaker_active = False
        trade_counter = 0

        # Load and combine all datasets chronologically
        all_records = []
        for pf in parquet_files:
            try:
                df = pd.read_parquet(pf)
                valid, report, recs = HistoricalDataValidator.validate_dataframe(df, file_path=pf)
                if valid:
                    all_records.extend(recs)
            except Exception as e:
                log.error(f"Error reading {pf}: {e}")

        if not all_records:
            return metrics

        # Reconstruct snapshots
        (
            snapshots,
            _,
            _,
            _,
            _,
            _,
            _
        ) = HistoricalDataValidator.reconstruct_snapshots(all_records)

        log.info(f"Reconstructed {len(snapshots)} chronological snapshots across {len(parquet_files)} expiries.")
        last_snap = None

        for snap in snapshots:
            ts_str = snap["timestamp"]
            dt_obj = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            trade_date = dt_obj.date()
            time_str = dt_obj.strftime("%H:%M:%S")
            expiry = snap["expiry"]
            spot = snap["spot_price"]
            chain = snap["chain_by_strike"]

            # 1. Reset daily circuit breaker and settle unclosed trades on new trading session
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

            # 2. Update and Check Open Trades
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
                        # Profit Target: spread buyback cost reaches 30% of original credit (capturing 70% of credit)
                        target_cost_threshold = round(trade.net_credit_pts * (1.0 - self.target_profit_pct), 2)
                        # Stop Loss: spread buyback cost reaches 150% of original credit (loss of 50% of original credit)
                        stop_cost_threshold = round(trade.net_credit_pts * (1.0 + self.stop_loss_credit_mult), 2)

                        target_hit = (current_cost <= target_cost_threshold)
                        stop_hit = (current_cost >= stop_cost_threshold)

                        if target_hit and stop_hit:
                            # Conservative ambiguity policy: If quotes/spread indicate both thresholds breached in the snapshot,
                            # assume adverse outcome (STOP_LOSS).
                            exit_reason = "STOP_LOSS"
                            exit_cost_pts = current_cost
                        elif target_hit:
                            exit_reason = "TARGET"
                            exit_cost_pts = current_cost
                        elif stop_hit:
                            exit_reason = "STOP_LOSS"
                            exit_cost_pts = current_cost

                if exit_reason and exit_cost_pts is not None:
                    # Finalize trade
                    trade.exit_time = ts_str
                    trade.exit_reason = exit_reason
                    trade.exit_spread_cost = exit_cost_pts
                    try:
                        t_in = datetime.strptime(trade.entry_time, "%Y-%m-%d %H:%M:%S")
                        t_out = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        trade.holding_time_mins = round((t_out - t_in).total_seconds() / 60.0, 1)
                    except Exception:
                        trade.holding_time_mins = 0.0

                    # P&L Calculation
                    gross_pnl = (trade.net_credit_pts - exit_cost_pts) * trade.quantity
                    trade.gross_pnl = round(gross_pnl, 2)

                    # Exit transaction costs
                    # Reconstruct individual legs for accurate leg-by-leg brokerage and turnover fees
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
                    else:
                        # Expiry settlement: cash-settled without exchange order brokerage
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

                    # Slippage cost attribution (tracked as informational metadata; scenario fills already reflect slippage)
                    num_legs = 4 if trade.strategy_id == "IRON_CONDOR" else 2
                    slippage_rate = 0.00 if scenario == "OPTIMISTIC" else 0.05 if scenario == "BASE" else 0.10
                    trade.slippage_cost = round(slippage_rate * num_legs * trade.quantity, 2)

                    # Net P&L = Gross P&L minus transaction costs (avoid duplicate slippage deduction)
                    net_pnl = trade.gross_pnl - trade.total_costs
                    trade.net_pnl = round(net_pnl, 2)

                    # Update equity & tracking
                    current_equity += trade.net_pnl
                    if trade.net_pnl < 0:
                        daily_loss_incurred += abs(trade.net_pnl)
                        consecutive_losses += 1
                        # 15-minute cooldown initiated after 1 qualifying loss exit (authoritative production rule)
                        cooldown_active_until = dt_obj + timedelta(minutes=Config.CONSECUTIVE_LOSS_COOLDOWN_MINS)
                    else:
                        consecutive_losses = 0

                    if daily_loss_incurred >= Config.get_max_daily_loss_inr():
                        circuit_breaker_active = True

                    closed_trades_this_tick.append(trade)

            for ct in closed_trades_this_tick:
                open_trades.remove(ct)
                # Record in metrics
                self._record_trade(metrics[ct.strategy_id], ct)
                self._record_trade(metrics["COMBINED"], ct)

            # 3. Entry Evaluation
            # Safety guards:
            # - Limit open trades to max 2 concurrent spreads (Config.MAX_INTRADAY_POSITIONS)
            # - Daily loss circuit breaker (Config.get_max_daily_loss_inr())
            # - Consecutive loss cooldown (Config.CONSECUTIVE_LOSS_COOLDOWN_MINS)
            # - Session entry window: 09:20:00 to 14:45:00
            if len(open_trades) >= Config.MAX_INTRADAY_POSITIONS or circuit_breaker_active or cooldown_active_until:
                continue

            if time_str < "09:20:00" or time_str > "14:45:00":
                continue

            # Run production analyzer & strategy selector
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
                selection = AutoStrategySelector.evaluate_auto_selection(analysis, chain)

                if selection.selection_type != "NONE" and selection.selected_strategies:
                    for cand in selection.selected_strategies:
                        if cand.strategy_id not in self.approved_strategies:
                            continue
                        if not cand.is_eligible or cand.net_credit_pts <= 0:
                            continue

                        # Check if strategy already open
                        if any(t.strategy_id == cand.strategy_id for t in open_trades):
                            continue

                        # Check maximum concurrent positions limit
                        if len(open_trades) >= Config.MAX_INTRADAY_POSITIONS:
                            break

                        # Lot size from official ContractRegistry
                        lot_size = ContractRegistry.get_contract_lot_size("NIFTY", expiry)
                        quantity = lot_size * cand.lots

                        # Calculate entry execution fill
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

                        # Scenario-specific entry net credit
                        sell_credit = sum(l["price"] for l in legs_costs if l["action"] == "SELL")
                        buy_debit = sum(l["price"] for l in legs_costs if l["action"] == "BUY")
                        scenario_net_credit = round(sell_credit - buy_debit, 2)
                        if scenario_net_credit <= 0:
                            continue

                        # Entry transaction costs
                        entry_costs = TransactionCostEngine.calculate_order_costs_breakdown(
                            legs_costs, lots=cand.lots, trade_date=trade_date
                        )
                        entry_brokerage = entry_costs["brokerage"]
                        entry_taxes = entry_costs["total"] - entry_brokerage
                        entry_total = entry_costs["total"]

                        # Calculate trade maximum risk (including round-trip costs) matching production convention
                        wing_width = cand.strike_width
                        trade_max_loss_pts = max(0.0, wing_width - scenario_net_credit)
                        trade_max_loss_inr = round(trade_max_loss_pts * quantity, 2)
                        trade_risk_inr = getattr(cand, "total_risk_with_costs_inr", trade_max_loss_inr)
                        estimated_round_trip_costs = entry_total * 2.0
                        scenario_trade_risk_inr = round(trade_max_loss_inr + estimated_round_trip_costs, 2)
                        effective_trade_risk = max(trade_risk_inr, scenario_trade_risk_inr)

                        # Enforce per-trade maximum risk limit (₹5,000 / Config.get_max_risk_per_trade_inr())
                        if effective_trade_risk > Config.get_max_risk_per_trade_inr():
                            continue

                        # Enforce combined portfolio risk limit (₹20,000 / Config.get_max_combined_portfolio_risk_inr())
                        current_portfolio_risk = sum(t.max_risk_inr for t in open_trades)
                        projected_portfolio_risk = current_portfolio_risk + effective_trade_risk
                        if projected_portfolio_risk > Config.get_max_combined_portfolio_risk_inr():
                            continue

                        trade_counter += 1
                        trade_id = f"BT-{scenario[:3]}-{trade_counter:04d}"

                        # Extract strikes
                        short_k = cand.legs[0].strike
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
                            spot_cushion_pts=round(abs(spot - short_k), 2),
                            credit_to_width_pct=round((scenario_net_credit / cand.strike_width) * 100.0, 2) if cand.strike_width > 0 else 0.0,
                            pcr_oi_entry=round(analysis.pcr_oi, 2),
                        )
                        open_trades.append(new_trade)
                        if len(open_trades) >= Config.MAX_INTRADAY_POSITIONS:
                            break

            except Exception as e:
                log.debug(f"Snapshot analysis skipped: {e}")
                continue

        # Close any lingering open trades at the very end of simulation
        if open_trades and last_snap:
            for et in list(open_trades):
                self._close_trade_at_settlement(et, last_snap, scenario, reason="SESSION_CUTOFF")
                current_equity += et.net_pnl
                open_trades.remove(et)
                self._record_trade(metrics[et.strategy_id], et)
                self._record_trade(metrics["COMBINED"], et)

        # Finalize metrics
        for strat_key, m in metrics.items():
            self._finalize_metrics(m)

        return metrics

    def _record_trade(self, m: StrategyMetrics, trade: BacktestTrade) -> None:
        """Appends a closed trade to metrics tracker."""
        m.trades.append(trade)
        m.total_trades += 1
        m.gross_pnl += trade.gross_pnl
        m.total_brokerage += trade.total_brokerage
        m.total_taxes += trade.total_taxes
        m.total_costs += trade.total_costs
        m.total_slippage += trade.slippage_cost
        m.net_pnl += trade.net_pnl

        if trade.net_pnl > 0:
            m.winning_trades += 1
            if trade.net_pnl > m.largest_win_inr:
                m.largest_win_inr = trade.net_pnl
        else:
            m.losing_trades += 1
            if abs(trade.net_pnl) > m.largest_loss_inr:
                m.largest_loss_inr = abs(trade.net_pnl)

        # Monthly breakdown
        month_str = trade.entry_time[:7]  # YYYY-MM
        m.monthly_pnl[month_str] = m.monthly_pnl.get(month_str, 0.0) + trade.net_pnl

        # Regime breakdown
        regime = trade.market_regime_entry
        m.pnl_by_regime[regime] = m.pnl_by_regime.get(regime, 0.0) + trade.net_pnl

    def _finalize_metrics(self, m: StrategyMetrics) -> None:
        """Calculates win rate, profit factor, drawdown, and expectancy."""
        if m.total_trades == 0:
            return

        m.win_rate_pct = round((m.winning_trades / m.total_trades) * 100.0, 2)
        m.roi_pct = round((m.net_pnl / self.starting_equity) * 100.0, 2)

        gains = sum(t.net_pnl for t in m.trades if t.net_pnl > 0)
        losses = sum(abs(t.net_pnl) for t in m.trades if t.net_pnl < 0)

        m.profit_factor = round(gains / losses, 2) if losses > 0 else 99.99

        avg_win = (gains / m.winning_trades) if m.winning_trades > 0 else 0.0
        avg_loss = (losses / m.losing_trades) if m.losing_trades > 0 else 0.0
        p_win = m.winning_trades / m.total_trades
        p_loss = m.losing_trades / m.total_trades
        m.expectancy_inr = round((p_win * avg_win) - (p_loss * avg_loss), 2)

        # Max Drawdown & Losing Streak
        cumulative_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        curr_streak = 0
        max_streak = 0

        for t in m.trades:
            cumulative_pnl += t.net_pnl
            if cumulative_pnl > peak:
                peak = cumulative_pnl
            dd = peak - cumulative_pnl
            if dd > max_dd:
                max_dd = dd

            if t.net_pnl < 0:
                curr_streak += 1
                if curr_streak > max_streak:
                    max_streak = curr_streak
            else:
                curr_streak = 0

        m.max_drawdown_inr = round(max_dd, 2)
        m.max_drawdown_pct = round((max_dd / self.starting_equity) * 100.0, 2)
        m.max_consecutive_losses = max_streak
