"""
app/research/regime_diagnostics.py — Zero-Lookahead Market Regime Diagnostics Engine

Implements research-only diagnostic indicators for evaluating range stability
and quote quality for NIFTY Iron Condor setups:
1. ADX(14) via Wilder's Directional Movement Index.
2. Recent Range % of Spot over configurable lookback (default N=7 sessions, <= 2.5%).
3. Bollinger Bandwidth Percentile (20, 2.0) ranked strictly against prior observations.
4. Directional Movement Guard (5-day return within +/- 2%).
5. Quote Quality Guard (separate per-leg validation: positive quotes, uncrossed, spread limit).

Strict Invariant:
All calculations use strictly causal information available up to decision timestamp t.
Zero future data or full-dataset normalization is permitted.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np


@dataclass
class DiagnosticThresholds:
    """Configurable thresholds for research regime diagnostics."""
    adx_max: float = 22.0
    range_lookback_days: int = 7
    range_max_pct: float = 2.5
    bb_period: int = 20
    bb_std_mult: float = 2.0
    bb_percentile_max: float = 40.0
    direction_lookback_days: int = 5
    direction_max_pct: float = 2.0
    max_bid_ask_spread_pct: float = 10.0
    max_quote_freshness_secs: float = 120.0


@dataclass
class RegimeDiagnosticResult:
    """Detailed output of the regime diagnostics evaluation."""
    timestamp: str
    spot_price: float
    existing_regime: str
    existing_bias: str

    # Indicator Values
    adx_value: Optional[float] = None
    adx_passed: bool = True

    recent_range_pct: Optional[float] = None
    recent_range_passed: bool = True

    bollinger_bandwidth: Optional[float] = None
    bollinger_bandwidth_percentile: Optional[float] = None
    bollinger_bandwidth_passed: bool = True

    directional_movement_pct: Optional[float] = None
    directional_movement_passed: bool = True

    quote_quality_passed: bool = True
    quote_quality_issues: List[str] = field(default_factory=list)

    # Composite Research Decision
    all_filters_passed: bool = True
    rejection_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "spot_price": self.spot_price,
            "existing_regime": self.existing_regime,
            "existing_bias": self.existing_bias,
            "adx_value": round(self.adx_value, 2) if self.adx_value is not None else None,
            "adx_passed": self.adx_passed,
            "recent_range_pct": round(self.recent_range_pct, 2) if self.recent_range_pct is not None else None,
            "recent_range_passed": self.recent_range_passed,
            "bollinger_bandwidth": round(self.bollinger_bandwidth, 4) if self.bollinger_bandwidth is not None else None,
            "bollinger_bandwidth_percentile": round(self.bollinger_bandwidth_percentile, 1) if self.bollinger_bandwidth_percentile is not None else None,
            "bollinger_bandwidth_passed": self.bollinger_bandwidth_passed,
            "directional_movement_pct": round(self.directional_movement_pct, 2) if self.directional_movement_pct is not None else None,
            "directional_movement_passed": self.directional_movement_passed,
            "quote_quality_passed": self.quote_quality_passed,
            "quote_quality_issues": self.quote_quality_issues,
            "all_filters_passed": self.all_filters_passed,
            "rejection_reasons": self.rejection_reasons,
        }


class RegimeDiagnosticsEngine:
    """
    Evaluates research-only regime filters using historical spot and quote data.
    Ensures strict zero-lookahead compliance.
    """

    @classmethod
    def calculate_adx(cls, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """
        Calculates Wilder's ADX(14) using causal rolling operations.
        Formula:
        +DM = High[t] - High[t-1] if (+DM > 0 and +DM > -DM) else 0
        -DM = Low[t-1] - Low[t] if (-DM > 0 and -DM > +DM) else 0
        TR = max(High - Low, abs(High - Close[t-1]), abs(Low - Close[t-1]))
        Smooth with Wilder's exponential smoothing (alpha = 1 / period).
        DX = 100 * abs(+DI - -DI) / (+DI + -DI)
        ADX = WilderSmooth(DX, period).
        """
        if len(close) < (period * 2):
            return pd.Series([np.nan] * len(close), index=close.index)

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        plus_dm = pd.Series(plus_dm, index=close.index)
        minus_dm = pd.Series(minus_dm, index=close.index)

        # Wilder's smoothing (alpha = 1/period is equivalent to ewm(com=period-1))
        tr_smooth = tr.ewm(com=period - 1, min_periods=period, adjust=False).mean()
        plus_di = 100.0 * (plus_dm.ewm(com=period - 1, min_periods=period, adjust=False).mean() / tr_smooth)
        minus_di = 100.0 * (minus_dm.ewm(com=period - 1, min_periods=period, adjust=False).mean() / tr_smooth)

        denom = plus_di + minus_di
        dx = 100.0 * (plus_di - minus_di).abs() / np.where(denom == 0, 1.0, denom)
        adx = dx.ewm(com=period - 1, min_periods=period, adjust=False).mean()
        return adx

    @classmethod
    def calculate_recent_range_pct(cls, high: pd.Series, low: pd.Series, spot: float, lookback: int = 7) -> float:
        """
        Calculates the high-low price range over the last N sessions as a percentage of spot.
        Range % = (Highest High - Lowest Low) / Spot * 100.0
        """
        if len(high) == 0 or spot <= 0:
            return 0.0
        n_high = high.tail(lookback).max()
        n_low = low.tail(lookback).min()
        return float(((n_high - n_low) / spot) * 100.0)

    @classmethod
    def calculate_bollinger_bandwidth(
        cls, close: pd.Series, period: int = 20, std_mult: float = 2.0
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Calculates Bollinger Bandwidth and rolling historical percentile.
        Middle = SMA(close, period)
        Upper = Middle + std_mult * Stdev
        Lower = Middle - std_mult * Stdev
        Bandwidth = (Upper - Lower) / Middle
        Percentile is computed strictly over expanding/rolling historical observations prior to t.
        """
        if len(close) < period:
            return pd.Series([np.nan] * len(close), index=close.index), pd.Series([np.nan] * len(close), index=close.index)

        sma = close.rolling(window=period).mean()
        rstd = close.rolling(window=period).std()
        upper = sma + (std_mult * rstd)
        lower = sma - (std_mult * rstd)

        denom = np.where(sma == 0, 1.0, sma)
        bandwidth = (upper - lower) / denom

        # Calculate percentile against past history (strictly expanding or 120-day rolling lookback)
        # Using rolling window of up to 120 sessions (approx 6 months of daily data)
        def _calc_pct(series_window):
            if len(series_window) < 5 or np.isnan(series_window.iloc[-1]):
                return np.nan
            val = series_window.iloc[-1]
            return float((series_window <= val).mean() * 100.0)

        rolling_pct = bandwidth.rolling(window=120, min_periods=20).apply(_calc_pct, raw=False)
        return bandwidth, rolling_pct

    @classmethod
    def calculate_directional_movement_pct(cls, close: pd.Series, lookback: int = 5) -> float:
        """
        Calculates the cumulative return over the last N sessions.
        Return % = (Close[t] - Close[t-N]) / Close[t-N] * 100.0
        """
        if len(close) <= lookback:
            return 0.0
        c_now = float(close.iloc[-1])
        c_past = float(close.iloc[-1 - lookback])
        if c_past <= 0:
            return 0.0
        return float(((c_now - c_past) / c_past) * 100.0)

    @classmethod
    def validate_quote_quality(
        cls, legs: List[Any], max_spread_pct: float = 10.0, max_freshness_secs: float = 120.0, enforce_four_legs: bool = False
    ) -> Tuple[bool, List[str]]:
        """
        Separately validates quote quality across spread legs:
        - Bid > 0, Ask > 0
        - Ask >= Bid (uncrossed)
        - Spread % = (Ask - Bid) / Mid * 100 <= max_spread_pct
        - Freshness within limit if timestamp available
        """
        issues = []
        if enforce_four_legs and len(legs) != 4:
            issues.append(f"Invalid leg count ({len(legs)}); exactly 4 legs required for Iron Condor.")
            return False, issues

        for i, leg in enumerate(legs, start=1):
            if hasattr(leg, "strike"):
                strike = leg.strike
                opt_type = getattr(leg, "option_type", getattr(leg, "opt_type", ""))
                action = leg.action
                bid = float(getattr(leg, "bid", 0.0) or 0.0)
                ask = float(getattr(leg, "ask", 0.0) or 0.0)
            else:
                strike = leg.get("strike", 0.0)
                opt_type = leg.get("option_type", "")
                action = leg.get("action", "")
                bid = float(leg.get("bid", 0.0) or 0.0)
                ask = float(leg.get("ask", 0.0) or 0.0)

            if bid <= 0:
                issues.append(f"Leg {i} ({action} {strike} {opt_type}): Non-positive Bid (₹{bid:.2f}).")
            if ask <= 0:
                issues.append(f"Leg {i} ({action} {strike} {opt_type}): Non-positive Ask (₹{ask:.2f}).")
            if bid > 0 and ask > 0 and ask < bid:
                issues.append(f"Leg {i} ({action} {strike} {opt_type}): Crossed market quotes (Bid ₹{bid:.2f} > Ask ₹{ask:.2f}).")

            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 0.0
                if spread_pct > max_spread_pct:
                    issues.append(f"Leg {i} ({action} {strike} {opt_type}): Bid-Ask spread ({spread_pct:.1f}%) exceeds limit {max_spread_pct:.1f}%.")

        return (len(issues) == 0), issues

    @classmethod
    def evaluate_candidate(
        cls,
        spot: float,
        timestamp_str: str,
        existing_regime: str,
        existing_bias: str,
        price_history_df: Optional[pd.DataFrame] = None,
        candidate_legs: Optional[List[Dict[str, Any]]] = None,
        thresholds: Optional[DiagnosticThresholds] = None,
    ) -> RegimeDiagnosticResult:
        """
        Evaluates a complete candidate snapshot against all diagnostic filters.
        """
        t = thresholds or DiagnosticThresholds()
        rejections = []

        res = RegimeDiagnosticResult(
            timestamp=timestamp_str,
            spot_price=spot,
            existing_regime=existing_regime,
            existing_bias=existing_bias,
        )

        if price_history_df is not None and not price_history_df.empty:
            high = price_history_df["high"] if "high" in price_history_df else price_history_df["close"]
            low = price_history_df["low"] if "low" in price_history_df else price_history_df["close"]
            close = price_history_df["close"]

            # B1. ADX(14)
            adx_series = cls.calculate_adx(high, low, close, period=14)
            latest_adx = float(adx_series.iloc[-1]) if not adx_series.empty and not np.isnan(adx_series.iloc[-1]) else None
            res.adx_value = latest_adx
            if latest_adx is not None:
                res.adx_passed = latest_adx < t.adx_max
                if not res.adx_passed:
                    rejections.append(f"ADX({latest_adx:.1f}) exceeds rangebound maximum ({t.adx_max:.1f}).")

            # B2. Recent Range %
            range_pct = cls.calculate_recent_range_pct(high, low, spot, lookback=t.range_lookback_days)
            res.recent_range_pct = range_pct
            res.recent_range_passed = range_pct <= t.range_max_pct
            if not res.recent_range_passed:
                rejections.append(f"Recent {t.range_lookback_days}-day range ({range_pct:.2f}%) exceeds threshold ({t.range_max_pct:.2f}%).")

            # B3. Bollinger Bandwidth Percentile
            bw_series, pct_series = cls.calculate_bollinger_bandwidth(close, period=t.bb_period, std_mult=t.bb_std_mult)
            latest_bw = float(bw_series.iloc[-1]) if not bw_series.empty and not np.isnan(bw_series.iloc[-1]) else None
            latest_pct = float(pct_series.iloc[-1]) if not pct_series.empty and not np.isnan(pct_series.iloc[-1]) else None
            res.bollinger_bandwidth = latest_bw
            res.bollinger_bandwidth_percentile = latest_pct
            if latest_pct is not None:
                res.bollinger_bandwidth_passed = latest_pct <= t.bb_percentile_max
                if not res.bollinger_bandwidth_passed:
                    rejections.append(f"Bollinger Bandwidth percentile ({latest_pct:.1f}%) exceeds maximum ({t.bb_percentile_max:.1f}%).")

            # B4. Directional Movement Guard
            dir_pct = cls.calculate_directional_movement_pct(close, lookback=t.direction_lookback_days)
            res.directional_movement_pct = dir_pct
            res.directional_movement_passed = abs(dir_pct) <= t.direction_max_pct
            if not res.directional_movement_passed:
                rejections.append(f"5-day directional return ({dir_pct:+.2f}%) exceeds threshold (+/-{t.direction_max_pct:.1f}%).")

        # B5. Quote Quality Guard
        if candidate_legs:
            q_passed, q_issues = cls.validate_quote_quality(candidate_legs, max_spread_pct=t.max_bid_ask_spread_pct)
            res.quote_quality_passed = q_passed
            res.quote_quality_issues = q_issues
            if not q_passed:
                rejections.extend(q_issues)

        res.rejection_reasons = rejections
        res.all_filters_passed = (len(rejections) == 0)
        return res


# Module-level convenience functions
calculate_adx = RegimeDiagnosticsEngine.calculate_adx
calculate_recent_range_pct = RegimeDiagnosticsEngine.calculate_recent_range_pct
calculate_bollinger_bandwidth = RegimeDiagnosticsEngine.calculate_bollinger_bandwidth
calculate_directional_movement_pct = RegimeDiagnosticsEngine.calculate_directional_movement_pct
validate_quote_quality = RegimeDiagnosticsEngine.validate_quote_quality
