"""
app/option_chain_service.py — Advanced Option Chain Service (Live & Historical Time-Travel)
Powers the OI Pulse-style option chain analysis with:
1. Live broker stream vs. Historical session replay with exact point-in-time time-travel (e.g. 12:00:00).
2. Dynamic Support & Resistance Walls:
   - Call Max OTM OI -> Primary Resistance (R1 Wall)
   - Call 2nd Max OTM OI -> Secondary Resistance (R2 Wall)
   - Put Max OTM OI -> Primary Support (S1 Wall)
   - Put 2nd Max OTM OI -> Secondary Support (S2 Wall)
3. Greeks (Black-76 Delta), Open=High / Open=Low flags, OI Interpretations (LB, SB, SC, LU).
4. Strike-wise PCR and Total Option Chain PCR.
5. In-cell bar percentage gauges and extreme OI change highlighting.
6. 7-Day multi-day historical OHLC and OI build-up modal query.
"""

import os
import glob
import math
import logging
from datetime import datetime, date
from statistics import NormalDist
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Config
from app.market_data import MarketDataProvider

log = logging.getLogger("option_chain_service")
IST = ZoneInfo("Asia/Kolkata")
_norm = NormalDist(mu=0.0, sigma=1.0)


class OptionChainService:
    """Authoritative service for live and historical option chain analysis."""

    @classmethod
    def _calculate_delta(cls, spot: float, strike: float, dte_days: float, iv_pct: float, is_call: bool) -> float:
        """Calculates Black-76 Delta."""
        try:
            if spot <= 0 or strike <= 0:
                return 0.50 if is_call else -0.50
            T = max(1.0 / 365.0, (dte_days or 5.0) / 365.0)
            sigma = max(0.05, (iv_pct or 15.0) / 100.0)
            d1 = (math.log(spot / strike) + 0.5 * (sigma ** 2) * T) / (sigma * math.sqrt(T))
            cdf_val = _norm.cdf(d1)
            return round(cdf_val, 2) if is_call else round(cdf_val - 1.0, 2)
        except Exception:
            return 0.50 if is_call else -0.50

    @classmethod
    def _get_oi_interpretation(cls, ltp_chg: float, oi_chg: float) -> str:
        """
        Classifies strike writer flow:
        - Price Up + OI Up   -> LB (Long Build-up)
        - Price Down + OI Up -> SB (Short Build-up)
        - Price Up + OI Down -> SC (Short Covering)
        - Price Down + OI Down -> LU (Long Unwinding)
        """
        if oi_chg > 0:
            return "LB" if ltp_chg >= 0 else "SB"
        elif oi_chg < 0:
            return "SC" if ltp_chg >= 0 else "LU"
        return "--"

    @classmethod
    def get_available_dates(cls) -> Dict[str, Any]:
        """
        Returns list of all available historical dates from instance/historical_data/*.parquet.
        """
        pattern = os.path.join(Config.HISTORICAL_DATA_DIR, "NIFTY_*.parquet")
        files = sorted(glob.glob(pattern), reverse=True)
        dates = []
        for f in files:
            base = os.path.basename(f)
            # e.g. NIFTY_2026-10-19.parquet -> 2026-10-19
            clean_date = base.replace("NIFTY_", "").replace(".parquet", "")
            if len(clean_date) == 10 and clean_date.count("-") == 2:
                dates.append(clean_date)

        return {
            "symbol": "NIFTY",
            "available_dates": dates,
            "total_sessions": len(dates),
            "latest_date": dates[0] if dates else None,
        }

    @classmethod
    def get_historical_chain(
        cls,
        date_str: str,
        time_str: Optional[str] = None,
        expiry: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Loads historical snapshot for date_str at exact time_str (e.g. '12:00:00').
        """
        file_path = os.path.join(Config.HISTORICAL_DATA_DIR, f"NIFTY_{date_str}.parquet")
        if not os.path.exists(file_path):
            return {
                "status": "error",
                "message": f"Historical data file for date {date_str} not found.",
                "chain": [],
            }

        try:
            df = pd.read_parquet(file_path)
            if df.empty:
                return {"status": "error", "message": "Historical file is empty.", "chain": []}

            # Normalize timestamp to string
            df["ts_str"] = df["timestamp"].astype(str)
            all_timestamps = sorted(df["ts_str"].unique().tolist())
            available_times = [t.split(" ")[1] if " " in t else t for t in all_timestamps]

            # Determine target snapshot timestamp
            if time_str and time_str.strip():
                # Allow '12:00' or '12:00:00'
                clean_time = time_str.strip()
                if len(clean_time) == 5:
                    clean_time += ":00"
                target_ts = f"{date_str} {clean_time}"

                # Match exact or nearest preceding timestamp
                matching_ts = [t for t in all_timestamps if t <= target_ts]
                selected_ts = matching_ts[-1] if matching_ts else all_timestamps[0]
            else:
                selected_ts = all_timestamps[-1]  # Default to day's final snapshot (15:30)

            snap_df = df[df["ts_str"] == selected_ts]
            if snap_df.empty:
                snap_df = df[df["ts_str"] == all_timestamps[0]]
                selected_ts = all_timestamps[0]

            spot_price = float(snap_df["spot_price"].iloc[0])
            active_expiry = str(snap_df["expiry"].iloc[0])

            # Calculate session-level baseline for change calculations (from 09:15)
            first_ts = all_timestamps[0]
            first_df = df[df["ts_str"] == first_ts]
            day_open_spot = float(first_df["spot_price"].iloc[0]) if not first_df.empty else spot_price
            spot_change = round(spot_price - day_open_spot, 2)
            spot_change_pct = round((spot_change / day_open_spot * 100.0) if day_open_spot > 0 else 0.0, 2)

            # Group quotes by strike
            strikes = sorted(snap_df["strike"].unique().tolist())
            atm_strike = round(spot_price / 50.0) * 50.0

            # Calculate DTE
            try:
                exp_dt = datetime.strptime(active_expiry, "%Y-%m-%d").date()
                curr_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
                dte = max(1.0, float((exp_dt - curr_dt).days))
            except Exception:
                dte = 5.0

            # Pre-calculate min/max prices across the day for Open=High / Open=Low flags
            ohlc_by_sym = {}
            for sym, group in df.groupby("symbol"):
                ohlc_by_sym[sym] = {
                    "open": float(group["ltp"].iloc[0]),
                    "high": float(group["ltp"].max()),
                    "low": float(group["ltp"].min()),
                    "close": float(group["ltp"].iloc[-1]),
                }

            rows = []
            total_call_oi = 0
            total_put_oi = 0
            max_call_oi = 1
            max_put_oi = 1

            for stk in strikes:
                stk_df = snap_df[snap_df["strike"] == stk]
                ce_sub = stk_df[stk_df["option_type"] == "CE"]
                pe_sub = stk_df[stk_df["option_type"] == "PE"]

                ce_quote = cls._format_leg_data(ce_sub, ohlc_by_sym, spot_price, stk, dte, is_call=True)
                pe_quote = cls._format_leg_data(pe_sub, ohlc_by_sym, spot_price, stk, dte, is_call=False)

                total_call_oi += ce_quote["oi"]
                total_put_oi += pe_quote["oi"]

                max_call_oi = max(max_call_oi, ce_quote["oi"])
                max_put_oi = max(max_put_oi, pe_quote["oi"])

                pcr_val = round(pe_quote["oi"] / ce_quote["oi"], 2) if ce_quote["oi"] > 0 else 0.0

                rows.append({
                    "strike": stk,
                    "is_atm": abs(stk - atm_strike) < 25.0,
                    "ce_is_itm": stk < spot_price,
                    "pe_is_itm": stk > spot_price,
                    "pcr": pcr_val,
                    "ce": ce_quote,
                    "pe": pe_quote,
                })

            # Identify S1, S2, R1, R2 walls
            # R1: Highest Call OI strike in OTM (stk >= spot)
            # R2: 2nd Highest Call OI strike in OTM
            # S1: Highest Put OI strike in OTM (stk <= spot)
            # S2: 2nd Highest Put OI strike in OTM
            ce_otm_sorted = sorted([r for r in rows if r["strike"] >= spot_price], key=lambda x: x["ce"]["oi"], reverse=True)
            pe_otm_sorted = sorted([r for r in rows if r["strike"] <= spot_price], key=lambda x: x["pe"]["oi"], reverse=True)

            r1_strike = ce_otm_sorted[0]["strike"] if ce_otm_sorted else None
            r2_strike = ce_otm_sorted[1]["strike"] if len(ce_otm_sorted) > 1 else None
            s1_strike = pe_otm_sorted[0]["strike"] if pe_otm_sorted else None
            s2_strike = pe_otm_sorted[1]["strike"] if len(pe_otm_sorted) > 1 else None

            # Mark wall tags and calculate bar percentages
            max_pos_ce_chg = 0
            max_neg_ce_chg = 0
            max_pos_pe_chg = 0
            max_neg_pe_chg = 0

            for r in rows:
                stk = r["strike"]
                r["ce"]["wall"] = "R1" if stk == r1_strike else ("R2" if stk == r2_strike else None)
                r["pe"]["wall"] = "S1" if stk == s1_strike else ("S2" if stk == s2_strike else None)

                r["ce"]["oi_pct_bar"] = round((r["ce"]["oi"] / max_call_oi * 100.0), 1) if max_call_oi > 0 else 0
                r["pe"]["oi_pct_bar"] = round((r["pe"]["oi"] / max_put_oi * 100.0), 1) if max_put_oi > 0 else 0

                if r["ce"]["change_oi"] > max_pos_ce_chg:
                    max_pos_ce_chg = r["ce"]["change_oi"]
                if r["ce"]["change_oi"] < max_neg_ce_chg:
                    max_neg_ce_chg = r["ce"]["change_oi"]

                if r["pe"]["change_oi"] > max_pos_pe_chg:
                    max_pos_pe_chg = r["pe"]["change_oi"]
                if r["pe"]["change_oi"] < max_neg_pe_chg:
                    max_neg_pe_chg = r["pe"]["change_oi"]

            # Tag max positive/negative OI changes
            for r in rows:
                r["ce"]["is_max_pos_chg"] = (r["ce"]["change_oi"] == max_pos_ce_chg and max_pos_ce_chg > 0)
                r["ce"]["is_max_neg_chg"] = (r["ce"]["change_oi"] == max_neg_ce_chg and max_neg_ce_chg < 0)
                r["pe"]["is_max_pos_chg"] = (r["pe"]["change_oi"] == max_pos_pe_chg and max_pos_pe_chg > 0)
                r["pe"]["is_max_neg_chg"] = (r["pe"]["change_oi"] == max_neg_pe_chg and max_neg_pe_chg < 0)

            total_pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 0.0

            return {
                "status": "success",
                "mode": "historical",
                "symbol": "NIFTY",
                "date": date_str,
                "timestamp": selected_ts,
                "display_time": selected_ts.split(" ")[1] if " " in selected_ts else selected_ts,
                "available_times": available_times,
                "spot_price": spot_price,
                "spot_change": spot_change,
                "spot_change_pct": spot_change_pct,
                "expiry": active_expiry,
                "total_call_oi": total_call_oi,
                "total_put_oi": total_put_oi,
                "total_pcr": total_pcr,
                "s1_support": s1_strike,
                "s2_support": s2_strike,
                "r1_resistance": r1_strike,
                "r2_resistance": r2_strike,
                "atm_strike": atm_strike,
                "chain": rows,
            }

        except Exception as e:
            log.exception(f"Error loading historical chain: {e}")
            return {"status": "error", "message": str(e), "chain": []}

    @classmethod
    def _format_leg_data(
        cls,
        leg_df: pd.DataFrame,
        ohlc_by_sym: Dict[str, Dict[str, float]],
        spot: float,
        strike: float,
        dte: float,
        is_call: bool,
    ) -> Dict[str, Any]:
        """Formats and computes quote statistics for one option leg (CE or PE)."""
        if leg_df.empty:
            return {
                "symbol": "",
                "ltp": 0.0,
                "ltp_change": 0.0,
                "ltp_change_pct": 0.0,
                "bid": 0.0,
                "ask": 0.0,
                "oi": 0,
                "change_oi": 0,
                "change_oi_pct": 0.0,
                "volume": 0,
                "iv": 0.0,
                "iv_change": 0.0,
                "delta": 0.0,
                "open_equals_low": False,
                "open_equals_high": False,
                "oi_interpretation": "--",
                "oi_pct_bar": 0.0,
            }

        row = leg_df.iloc[0]
        sym = str(row.get("symbol", ""))
        ltp = float(row.get("ltp", 0.0))
        bid = float(row.get("bid", 0.0))
        ask = float(row.get("ask", 0.0))
        oi = int(row.get("oi", 0))
        change_oi = int(row.get("change_oi", 0))
        vol = int(row.get("volume", 0))
        iv = float(row.get("iv", 15.0))

        # Check OHLC
        ohlc = ohlc_by_sym.get(sym, {})
        op = ohlc.get("open", ltp)
        hi = ohlc.get("high", ltp)
        lo = ohlc.get("low", ltp)

        ltp_change = round(ltp - op, 2)
        ltp_change_pct = round((ltp_change / op * 100.0) if op > 0 else 0.0, 2)
        change_oi_pct = round((change_oi / (oi - change_oi) * 100.0) if (oi - change_oi) > 0 else 0.0, 2)

        open_eq_low = bool(abs(op - lo) <= 0.20 and op > 0)
        open_eq_high = bool(abs(op - hi) <= 0.20 and op > 0)

        delta = cls._calculate_delta(spot, strike, dte, iv, is_call)
        interpretation = cls._get_oi_interpretation(ltp_change, change_oi)

        return {
            "symbol": sym,
            "ltp": ltp,
            "ltp_change": ltp_change,
            "ltp_change_pct": ltp_change_pct,
            "bid": bid,
            "ask": ask,
            "oi": oi,
            "change_oi": change_oi,
            "change_oi_pct": change_oi_pct,
            "volume": vol,
            "iv": iv,
            "iv_change": 0.0,
            "delta": delta,
            "open_equals_low": open_eq_low,
            "open_equals_high": open_eq_high,
            "oi_interpretation": interpretation,
            "oi_pct_bar": 0.0,
        }

    @classmethod
    def get_live_chain(cls, expiry: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetches live option chain, calculates Greeks, walls, in-cell bars, and interpretations.
        """
        raw_chain = MarketDataProvider.get_option_chain(expiry=expiry)
        spot_price = float(raw_chain.get("spot_price", 0.0))
        spot_change_pct = float(raw_chain.get("spot_change_pct", 0.0))
        active_expiry = str(raw_chain.get("expiry", ""))
        data_status = str(raw_chain.get("data_status", "DISCONNECTED"))
        chain_by_strike = raw_chain.get("chain_by_strike", {})

        if spot_price <= 0 or not chain_by_strike:
            return {
                "status": "warning",
                "mode": "live",
                "message": "Live FYERS feed is disconnected. Connect in Settings or switch to Historical Data.",
                "data_status": data_status,
                "chain": [],
                "spot_price": spot_price,
            }

        atm_strike = round(spot_price / 50.0) * 50.0
        now_ist = datetime.now(IST)

        # Estimate DTE
        try:
            exp_clean = active_expiry.strip().upper()
            exp_dt = datetime.strptime(exp_clean, "%d-%b-%Y").date()
            dte = max(1.0, float((exp_dt - now_ist.date()).days))
        except Exception:
            dte = 5.0

        strikes = sorted([float(s) for s in chain_by_strike.keys()])
        # Visible range: +/- 450 pts around ATM
        visible_strikes = [s for s in strikes if abs(s - atm_strike) <= 450]
        if not visible_strikes:
            visible_strikes = strikes

        rows = []
        total_call_oi = 0
        total_put_oi = 0
        max_call_oi = 1
        max_put_oi = 1

        for stk in visible_strikes:
            q = chain_by_strike.get(stk) or chain_by_strike.get(f"{stk:.1f}") or {}
            raw_ce = q.get("CE", {})
            raw_pe = q.get("PE", {})

            ce_ltp = float(raw_ce.get("ltp", 0.0))
            ce_ltp_chg = float(raw_ce.get("ltp_change", 0.0))
            ce_ltp_chgp = float(raw_ce.get("ltp_change_pct", 0.0))
            ce_oi = int(raw_ce.get("oi", 0))
            ce_chg_oi = int(raw_ce.get("change_oi", 0))
            ce_chg_oip = float(raw_ce.get("change_oi_pct", 0.0))
            ce_vol = int(raw_ce.get("volume", 0))
            ce_iv = float(raw_ce.get("iv", 15.0) or 15.0)

            pe_ltp = float(raw_pe.get("ltp", 0.0))
            pe_ltp_chg = float(raw_pe.get("ltp_change", 0.0))
            pe_ltp_chgp = float(raw_pe.get("ltp_change_pct", 0.0))
            pe_oi = int(raw_pe.get("oi", 0))
            pe_chg_oi = int(raw_pe.get("change_oi", 0))
            pe_chg_oip = float(raw_pe.get("change_oi_pct", 0.0))
            pe_vol = int(raw_pe.get("volume", 0))
            pe_iv = float(raw_pe.get("iv", 15.0) or 15.0)

            ce_delta = cls._calculate_delta(spot_price, stk, dte, ce_iv, is_call=True)
            pe_delta = cls._calculate_delta(spot_price, stk, dte, pe_iv, is_call=False)

            total_call_oi += ce_oi
            total_put_oi += pe_oi
            max_call_oi = max(max_call_oi, ce_oi)
            max_put_oi = max(max_put_oi, pe_oi)

            pcr_val = round(pe_oi / ce_oi, 2) if ce_oi > 0 else 0.0

            rows.append({
                "strike": stk,
                "is_atm": abs(stk - atm_strike) < 25.0,
                "ce_is_itm": stk < spot_price,
                "pe_is_itm": stk > spot_price,
                "pcr": pcr_val,
                "ce": {
                    "symbol": raw_ce.get("symbol", ""),
                    "ltp": ce_ltp,
                    "ltp_change": ce_ltp_chg,
                    "ltp_change_pct": ce_ltp_chgp,
                    "bid": float(raw_ce.get("bid", 0.0)),
                    "ask": float(raw_ce.get("ask", 0.0)),
                    "oi": ce_oi,
                    "change_oi": ce_chg_oi,
                    "change_oi_pct": ce_chg_oip,
                    "volume": ce_vol,
                    "iv": ce_iv,
                    "iv_change": 0.0,
                    "delta": ce_delta,
                    "open_equals_low": False,
                    "open_equals_high": False,
                    "oi_interpretation": cls._get_oi_interpretation(ce_ltp_chg, ce_chg_oi),
                },
                "pe": {
                    "symbol": raw_pe.get("symbol", ""),
                    "ltp": pe_ltp,
                    "ltp_change": pe_ltp_chg,
                    "ltp_change_pct": pe_ltp_chgp,
                    "bid": float(raw_pe.get("bid", 0.0)),
                    "ask": float(raw_pe.get("ask", 0.0)),
                    "oi": pe_oi,
                    "change_oi": pe_chg_oi,
                    "change_oi_pct": pe_chg_oip,
                    "volume": pe_vol,
                    "iv": pe_iv,
                    "iv_change": 0.0,
                    "delta": pe_delta,
                    "open_equals_low": False,
                    "open_equals_high": False,
                    "oi_interpretation": cls._get_oi_interpretation(pe_ltp_chg, pe_chg_oi),
                },
            })

        # Calculate S/R Walls
        ce_otm_sorted = sorted([r for r in rows if r["strike"] >= spot_price], key=lambda x: x["ce"]["oi"], reverse=True)
        pe_otm_sorted = sorted([r for r in rows if r["strike"] <= spot_price], key=lambda x: x["pe"]["oi"], reverse=True)

        r1_strike = ce_otm_sorted[0]["strike"] if ce_otm_sorted else None
        r2_strike = ce_otm_sorted[1]["strike"] if len(ce_otm_sorted) > 1 else None
        s1_strike = pe_otm_sorted[0]["strike"] if pe_otm_sorted else None
        s2_strike = pe_otm_sorted[1]["strike"] if len(pe_otm_sorted) > 1 else None

        max_pos_ce_chg = 0
        max_neg_ce_chg = 0
        max_pos_pe_chg = 0
        max_neg_pe_chg = 0

        for r in rows:
            stk = r["strike"]
            r["ce"]["wall"] = "R1" if stk == r1_strike else ("R2" if stk == r2_strike else None)
            r["pe"]["wall"] = "S1" if stk == s1_strike else ("S2" if stk == s2_strike else None)

            r["ce"]["oi_pct_bar"] = round((r["ce"]["oi"] / max_call_oi * 100.0), 1) if max_call_oi > 0 else 0
            r["pe"]["oi_pct_bar"] = round((r["pe"]["oi"] / max_put_oi * 100.0), 1) if max_put_oi > 0 else 0

            if r["ce"]["change_oi"] > max_pos_ce_chg:
                max_pos_ce_chg = r["ce"]["change_oi"]
            if r["ce"]["change_oi"] < max_neg_ce_chg:
                max_neg_ce_chg = r["ce"]["change_oi"]

            if r["pe"]["change_oi"] > max_pos_pe_chg:
                max_pos_pe_chg = r["pe"]["change_oi"]
            if r["pe"]["change_oi"] < max_neg_pe_chg:
                max_neg_pe_chg = r["pe"]["change_oi"]

        for r in rows:
            r["ce"]["is_max_pos_chg"] = (r["ce"]["change_oi"] == max_pos_ce_chg and max_pos_ce_chg > 0)
            r["ce"]["is_max_neg_chg"] = (r["ce"]["change_oi"] == max_neg_ce_chg and max_neg_ce_chg < 0)
            r["pe"]["is_max_pos_chg"] = (r["pe"]["change_oi"] == max_pos_pe_chg and max_pos_pe_chg > 0)
            r["pe"]["is_max_neg_chg"] = (r["pe"]["change_oi"] == max_neg_pe_chg and max_neg_pe_chg < 0)

        total_pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 0.0

        return {
            "status": "success",
            "mode": "live",
            "symbol": "NIFTY",
            "date": now_ist.strftime("%Y-%m-%d"),
            "timestamp": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "display_time": now_ist.strftime("%H:%M:%S"),
            "spot_price": spot_price,
            "spot_change": round(spot_price * spot_change_pct / 100.0, 2),
            "spot_change_pct": spot_change_pct,
            "expiry": active_expiry,
            "available_expiries": raw_chain.get("available_expiries", []),
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "total_pcr": total_pcr,
            "s1_support": s1_strike,
            "s2_support": s2_strike,
            "r1_resistance": r1_strike,
            "r2_resistance": r2_strike,
            "atm_strike": atm_strike,
            "chain": rows,
        }

    @classmethod
    def get_strike_history(cls, strike: float, end_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Extracts up to 7 previous sessions of daily OHLC, Volume, and OI for a specific strike.
        """
        pattern = os.path.join(Config.HISTORICAL_DATA_DIR, "NIFTY_*.parquet")
        all_files = sorted(glob.glob(pattern))
        if not all_files:
            return {"status": "error", "message": "No historical parquet files found.", "history": []}

        # Filter up to end_date
        target_files = []
        for f in all_files:
            d = os.path.basename(f).replace("NIFTY_", "").replace(".parquet", "")
            if end_date and d > end_date:
                continue
            target_files.append((d, f))

        # Take last 7 sessions
        target_files = target_files[-7:]
        history_records = []

        prev_ce_oi = 0
        prev_pe_oi = 0

        for d_str, f_path in target_files:
            try:
                df = pd.read_parquet(f_path)
                stk_df = df[df["strike"] == float(strike)]
                if stk_df.empty:
                    continue

                ce = stk_df[stk_df["option_type"] == "CE"]
                pe = stk_df[stk_df["option_type"] == "PE"]

                ce_open = float(ce["ltp"].iloc[0]) if not ce.empty else 0.0
                ce_high = float(ce["ltp"].max()) if not ce.empty else 0.0
                ce_low = float(ce["ltp"].min()) if not ce.empty else 0.0
                ce_close = float(ce["ltp"].iloc[-1]) if not ce.empty else 0.0
                ce_oi = int(ce["oi"].iloc[-1]) if not ce.empty else 0
                ce_vol = int(ce["volume"].sum()) if not ce.empty else 0

                pe_open = float(pe["ltp"].iloc[0]) if not pe.empty else 0.0
                pe_high = float(pe["ltp"].max()) if not pe.empty else 0.0
                pe_low = float(pe["ltp"].min()) if not pe.empty else 0.0
                pe_close = float(pe["ltp"].iloc[-1]) if not pe.empty else 0.0
                pe_oi = int(pe["oi"].iloc[-1]) if not pe.empty else 0
                pe_vol = int(pe["volume"].sum()) if not pe.empty else 0

                ce_oi_chg = (ce_oi - prev_ce_oi) if prev_ce_oi > 0 else 0
                pe_oi_chg = (pe_oi - prev_pe_oi) if prev_pe_oi > 0 else 0

                ce_interp = cls._get_oi_interpretation(ce_close - ce_open, ce_oi_chg)
                pe_interp = cls._get_oi_interpretation(pe_close - pe_open, pe_oi_chg)

                prev_ce_oi = ce_oi
                prev_pe_oi = pe_oi

                history_records.append({
                    "date": d_str,
                    "ce": {
                        "open": ce_open,
                        "high": ce_high,
                        "low": ce_low,
                        "close": ce_close,
                        "oi": ce_oi,
                        "oi_change": ce_oi_chg,
                        "volume": ce_vol,
                        "interpretation": ce_interp,
                    },
                    "pe": {
                        "open": pe_open,
                        "high": pe_high,
                        "low": pe_low,
                        "close": pe_close,
                        "oi": pe_oi,
                        "oi_change": pe_oi_chg,
                        "volume": pe_vol,
                        "interpretation": pe_interp,
                    },
                })
            except Exception as e:
                log.warning(f"Error reading {f_path} for strike {strike}: {e}")

        return {
            "status": "success",
            "strike": strike,
            "total_days": len(history_records),
            "history": history_records,
        }
