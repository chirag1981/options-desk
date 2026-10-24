"""
app/backtesting/data_validator.py — Historical Option Chain Dataset Validator & Ingestion Engine.

Validates historical CSV or Parquet option chain records for:
1. Schema conformity and required columns.
2. Chronological sequence (preventing look-ahead or time-travel bias).
3. Data integrity: non-negative prices, bid <= ask, positive spot, valid strikes.
4. Quote quality: executable bid/ask spreads vs zero/missing depth.
5. Snapshot reconstructibility for exact replay through MarketAnalyzer and AutoStrategySelector.
"""

import os
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Set
import pandas as pd

log = logging.getLogger("backtester.validator")

HISTORICAL_SCHEMA_COLUMNS = {
    "required": [
        "timestamp",    # YYYY-MM-DD HH:MM:SS or ISO-8601
        "spot_price",   # Underlying index spot price (float > 0)
        "expiry",       # Expiry date (e.g. '2026-10-27' or '27-OCT-2026')
        "strike",       # Option strike price (float > 0, e.g. 24500.0)
        "option_type",  # 'CE' or 'PE'
        "ltp",          # Last Traded Price (float >= 0)
        "bid",          # Executable Best Bid (float >= 0)
        "ask",          # Executable Best Ask (float >= 0, ask >= bid)
        "oi",           # Open Interest in contracts/shares (int >= 0)
    ],
    "optional": [
        "change_oi",       # Change in Open Interest (int, signed)
        "volume",          # Cumulative volume (int >= 0)
        "iv",              # Implied Volatility (float >= 0)
        "symbol",          # Contract symbol string
        "spot_change_pct", # Spot change % relative to prev close (float)
    ]
}


@dataclass
class DataQualityReport:
    """Detailed quality and coverage metrics for a historical dataset."""
    file_path: str
    file_format: str
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    unique_trading_days: int = 0
    unique_timestamps: int = 0
    unique_expiries: List[str] = field(default_factory=list)
    executable_bid_ask_pct: float = 0.0         # Rows with bid > 0 and ask > 0
    oi_coverage_pct: float = 0.0                # Rows with oi > 0
    change_oi_coverage_pct: float = 0.0         # Rows with non-null change_oi
    crossed_quotes_count: int = 0               # Rows where bid > ask
    non_chronological_records: int = 0          # Timestamps out of order
    records_normalized_chronologically: bool = False
    reconstructable_snapshots: int = 0          # Tier 1: Structurally reconstructable (ATM + min 5 paired strikes)
    adequately_covered_snapshots: int = 0       # Tier 2: Span >= 300 pts without peak OI truncated at boundary
    oi_truncation_warning_count: int = 0        # Snapshots where peak PE or CE OI sits at boundary strike
    actual_quotes_fillable_snapshots: int = 0   # Tier 3A: Positive actual executable bid/ask on candidate wings
    estimated_spread_fillable_snapshots: int = 0 # Tier 3B: Positive LTP present, requiring estimated spread assumptions
    fillable_snapshots_count: int = 0           # Legacy alias for actual_quotes_fillable_snapshots
    incomplete_snapshots_rejected: int = 0      # Snapshots lacking ATM or minimum OTM strike wings
    inconsistent_spot_snapshots: int = 0        # Snapshots with conflicting spot prices at identical timestamp
    is_acceptable_for_backtest: bool = False
    rejection_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HistoricalDataValidator:
    """
    Validates and reconstructs historical option chain datasets from CSV or Parquet files.
    Ensures zero synthetic data leakage into historical backtesting.
    """

    @classmethod
    def validate_file(cls, file_path: str) -> Tuple[bool, DataQualityReport, List[Dict[str, Any]]]:
        """
        Loads and validates a historical data file (.csv or .parquet).
        Returns (is_acceptable, report, list_of_valid_records).
        """
        if not os.path.exists(file_path):
            report = DataQualityReport(
                file_path=file_path,
                file_format="UNKNOWN",
                is_acceptable_for_backtest=False,
                rejection_reasons=[f"File not found: {file_path}"]
            )
            return False, report, []

        ext = os.path.splitext(file_path)[1].lower()
        if ext in (".parquet", ".pq"):
            file_format = "PARQUET"
            try:
                df = pd.read_parquet(file_path)
            except Exception as e:
                report = DataQualityReport(
                    file_path=file_path,
                    file_format="PARQUET",
                    is_acceptable_for_backtest=False,
                    rejection_reasons=[f"Failed to read Parquet file: {str(e)}"]
                )
                return False, report, []
        elif ext == ".csv":
            file_format = "CSV"
            try:
                df = pd.read_csv(file_path)
            except Exception as e:
                report = DataQualityReport(
                    file_path=file_path,
                    file_format="CSV",
                    is_acceptable_for_backtest=False,
                    rejection_reasons=[f"Failed to read CSV file: {str(e)}"]
                )
                return False, report, []
        else:
            report = DataQualityReport(
                file_path=file_path,
                file_format=ext,
                is_acceptable_for_backtest=False,
                rejection_reasons=[f"Unsupported file format '{ext}'. Must be .csv or .parquet"]
            )
            return False, report, []

        return cls.validate_dataframe(df, file_path=file_path, file_format=file_format)

    @classmethod
    def validate_dataframe(
        cls,
        df: pd.DataFrame,
        file_path: str = "<in-memory>",
        file_format: str = "DATAFRAME"
    ) -> Tuple[bool, DataQualityReport, List[Dict[str, Any]]]:
        """
        Validates a pandas DataFrame against the strict historical schema.
        """
        total_rows = len(df)
        report = DataQualityReport(
            file_path=file_path,
            file_format=file_format,
            total_rows=total_rows,
        )

        if total_rows == 0:
            report.rejection_reasons.append("Dataset is empty (0 rows).")
            return False, report, []

        # 1. Check Required Columns
        df_cols_lower = {str(c).strip().lower(): c for c in df.columns}
        missing_cols = []
        col_mapping = {}
        for req in HISTORICAL_SCHEMA_COLUMNS["required"]:
            if req.lower() not in df_cols_lower:
                missing_cols.append(req)
            else:
                col_mapping[req] = df_cols_lower[req.lower()]

        for opt in HISTORICAL_SCHEMA_COLUMNS["optional"]:
            if opt.lower() in df_cols_lower:
                col_mapping[opt] = df_cols_lower[opt.lower()]

        if missing_cols:
            report.rejection_reasons.append(f"Missing required columns: {', '.join(missing_cols)}")
            report.invalid_rows = total_rows
            return False, report, []

        # 2. Row-by-row validation & Sanitization
        valid_records: List[Dict[str, Any]] = []
        invalid_rows = 0
        crossed_quotes = 0
        executable_quotes = 0
        oi_count = 0
        change_oi_count = 0
        unique_timestamps: Set[str] = set()
        unique_trading_days: Set[str] = set()
        unique_expiries: Set[str] = set()
        last_dt = None
        non_chronological = 0

        # Pre-convert timestamps to test order
        try:
            ts_series = pd.to_datetime(df[col_mapping["timestamp"]])
        except Exception as e:
            report.rejection_reasons.append(f"Invalid timestamp format in dataset: {e}")
            return False, report, []

        for idx, row in df.iterrows():
            try:
                # Parse & Validate Timestamp
                raw_ts = ts_series.iloc[idx]
                if pd.isna(raw_ts):
                    invalid_rows += 1
                    continue
                dt_obj = raw_ts.to_pydatetime()
                ts_str = dt_obj.strftime("%Y-%m-%d %H:%M:%S")

                if last_dt is not None and dt_obj < last_dt:
                    non_chronological += 1
                last_dt = dt_obj

                unique_timestamps.add(ts_str)
                unique_trading_days.add(dt_obj.strftime("%Y-%m-%d"))

                # Spot Price
                spot = float(row[col_mapping["spot_price"]])
                if spot <= 0 or pd.isna(spot):
                    invalid_rows += 1
                    continue

                # Expiry
                expiry = str(row[col_mapping["expiry"]]).strip().upper()
                if not expiry or pd.isna(row[col_mapping["expiry"]]):
                    invalid_rows += 1
                    continue
                unique_expiries.add(expiry)

                # Strike
                strike = float(row[col_mapping["strike"]])
                if strike <= 0 or pd.isna(strike):
                    invalid_rows += 1
                    continue

                # Option Type
                opt_type = str(row[col_mapping["option_type"]]).strip().upper()
                if opt_type not in ("CE", "PE"):
                    invalid_rows += 1
                    continue

                # Prices & Quotes
                ltp = float(row[col_mapping["ltp"]]) if not pd.isna(row[col_mapping["ltp"]]) else 0.0
                bid = float(row[col_mapping["bid"]]) if not pd.isna(row[col_mapping["bid"]]) else 0.0
                ask = float(row[col_mapping["ask"]]) if not pd.isna(row[col_mapping["ask"]]) else 0.0

                if bid < 0 or ask < 0 or ltp < 0:
                    invalid_rows += 1
                    continue

                if bid > ask:
                    crossed_quotes += 1
                    invalid_rows += 1
                    continue

                if bid > 0 and ask > 0:
                    executable_quotes += 1

                # Open Interest
                raw_oi = row[col_mapping["oi"]]
                oi = int(raw_oi) if not pd.isna(raw_oi) and raw_oi is not None else 0
                if oi < 0:
                    invalid_rows += 1
                    continue
                if oi > 0:
                    oi_count += 1

                # Change in OI (distinguish missing None from 0)
                chg_oi = None
                if "change_oi" in col_mapping:
                    raw_chg = row[col_mapping["change_oi"]]
                    if not pd.isna(raw_chg) and raw_chg is not None:
                        try:
                            chg_oi = int(raw_chg)
                            change_oi_count += 1
                        except (ValueError, TypeError):
                            pass

                # Optional IV, Volume, and Spot Change % (preserve None when absent)
                iv = None
                if "iv" in col_mapping and not pd.isna(row[col_mapping["iv"]]):
                    try:
                        iv = float(row[col_mapping["iv"]])
                    except (ValueError, TypeError):
                        pass

                vol = None
                if "volume" in col_mapping and not pd.isna(row[col_mapping["volume"]]):
                    try:
                        vol = int(row[col_mapping["volume"]])
                    except (ValueError, TypeError):
                        pass

                spot_chg_pct = None
                if "spot_change_pct" in col_mapping and not pd.isna(row[col_mapping["spot_change_pct"]]):
                    try:
                        spot_chg_pct = float(row[col_mapping["spot_change_pct"]])
                    except (ValueError, TypeError):
                        pass

                rec = {
                    "timestamp": ts_str,
                    "spot_price": spot,
                    "spot_change_pct": spot_chg_pct,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": opt_type,
                    "ltp": ltp,
                    "bid": bid,
                    "ask": ask,
                    "oi": oi,
                    "change_oi": chg_oi,
                    "volume": vol,
                    "iv": iv,
                }
                valid_records.append(rec)

            except Exception:
                invalid_rows += 1
                continue

        # Safely sort chronologically to normalize input ordering
        if non_chronological > 0:
            valid_records.sort(key=lambda x: (x["timestamp"], x["expiry"], x["strike"], x["option_type"]))
            report.records_normalized_chronologically = True
            report.warnings.append(
                f"{non_chronological} records were out of chronological order and have been normalized chronologically."
            )

        report.valid_rows = len(valid_records)
        report.invalid_rows = invalid_rows
        report.crossed_quotes_count = crossed_quotes
        report.non_chronological_records = non_chronological
        report.unique_trading_days = len(unique_trading_days)
        report.unique_timestamps = len(unique_timestamps)
        report.unique_expiries = sorted(list(unique_expiries))

        if valid_records:
            report.start_time = min(r["timestamp"] for r in valid_records)
            report.end_time = max(r["timestamp"] for r in valid_records)
            report.executable_bid_ask_pct = round((executable_quotes / len(valid_records)) * 100.0, 2)
            report.oi_coverage_pct = round((oi_count / len(valid_records)) * 100.0, 2)
            report.change_oi_coverage_pct = round((change_oi_count / len(valid_records)) * 100.0, 2)

        # 3. Snapshot Reconstructibility Check
        (
            snapshots,
            incomplete_rejs,
            inconsistent_spots,
            actual_fillable,
            estimated_fillable,
            adequately_covered,
            oi_truncations,
        ) = cls.reconstruct_snapshots(valid_records)

        report.reconstructable_snapshots = len(snapshots)
        report.incomplete_snapshots_rejected = incomplete_rejs
        report.inconsistent_spot_snapshots = inconsistent_spots
        report.adequately_covered_snapshots = adequately_covered
        report.oi_truncation_warning_count = oi_truncations
        report.actual_quotes_fillable_snapshots = actual_fillable
        report.estimated_spread_fillable_snapshots = estimated_fillable
        report.fillable_snapshots_count = actual_fillable

        # 4. Acceptance Decisions
        if report.valid_rows == 0:
            report.rejection_reasons.append("Zero valid rows after data sanitization.")
        if report.reconstructable_snapshots == 0:
            report.rejection_reasons.append(
                "Zero reconstructable option-chain snapshots (requires ATM strike, >= 2 Put strikes below spot, >= 2 Call strikes above spot, and >= 5 paired strikes)."
            )
        if report.inconsistent_spot_snapshots > 0:
            report.warnings.append(
                f"{report.inconsistent_spot_snapshots} snapshots had conflicting spot prices at identical timestamp and were rejected."
            )
        if report.incomplete_snapshots_rejected > 0:
            report.warnings.append(
                f"{report.incomplete_snapshots_rejected} snapshots lacked complete ATM/wing coverage and were safely excluded from replay."
            )
        if report.oi_truncation_warning_count > 0:
            report.warnings.append(
                f"{report.oi_truncation_warning_count} snapshots have dominant Put/Call OI at the boundary strike or narrow span (<300 pts), indicating potential OI wall truncation."
            )
        if report.crossed_quotes_count > 0:
            report.warnings.append(f"{report.crossed_quotes_count} crossed quotes (bid > ask) were rejected.")
        if report.executable_bid_ask_pct < 50.0:
            report.warnings.append(
                f"Low executable bid/ask coverage ({report.executable_bid_ask_pct}%). Execution fills will be rejected where quotes are 0.0."
            )
        if report.estimated_spread_fillable_snapshots > 0 and report.actual_quotes_fillable_snapshots == 0:
            report.warnings.append(
                "Dataset provides LTP quotes only without bid/ask depth. Fills must be treated as ESTIMATED EXECUTION SCENARIOS and reported separately from actual-quote fills."
            )
        if report.change_oi_coverage_pct == 0.0:
            report.warnings.append(
                "Change in Open Interest (ΔOI) is entirely absent; strategy analysis will operate in reduced confidence mode."
            )

        report.is_acceptable_for_backtest = (len(report.rejection_reasons) == 0)
        return report.is_acceptable_for_backtest, report, valid_records

    @classmethod
    def reconstruct_snapshots(cls, records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int, int, int, int, int]:
        """
        Groups flat tabular option records into structured point-in-time Option Chains
        identical to the schema produced by MarketDataProvider.get_option_chain().
        Returns:
            (valid_snapshots, incomplete_rejected, inconsistent_spot_rejected,
             actual_fillable_count, estimated_fillable_count, adequately_covered_count, oi_truncation_count)
        """
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in records:
            key = (r["timestamp"], r["expiry"])
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(r)

        snapshots = []
        incomplete_rejected = 0
        inconsistent_spot_rejected = 0
        actual_fillable_count = 0
        estimated_fillable_count = 0
        adequately_covered_count = 0
        oi_truncation_count = 0

        for (ts_str, expiry), recs in sorted(grouped.items(), key=lambda x: x[0][0]):
            # Verify spot consistency across all rows in the exact same timestamp & expiry
            spots = [r["spot_price"] for r in recs]
            if max(spots) - min(spots) > 0.05:
                inconsistent_spot_rejected += 1
                continue

            spot = spots[0]
            spot_chg = recs[0].get("spot_change_pct")

            chain_by_strike: Dict[float, Dict[str, Any]] = {}
            for r in recs:
                stk = r["strike"]
                opt = r["option_type"]
                if stk not in chain_by_strike:
                    chain_by_strike[stk] = {"CE": {}, "PE": {}}
                chain_by_strike[stk][opt] = {
                    "symbol": f"NIFTY_{int(stk)}_{opt}",
                    "ltp": r["ltp"],
                    "bid": r["bid"],
                    "ask": r["ask"],
                    "oi": r["oi"],
                    "change_oi": r["change_oi"],
                    "volume": r.get("volume"),
                    "iv": r.get("iv"),
                }

            # 1. Completeness Verification (Tier 1: Structurally Reconstructable):
            # - At least 5 strikes with both CE and PE
            # - ATM strike must exist with both CE and PE
            # - At least 2 strikes below spot (for Bull Put S1 walls) and 2 strikes above spot (for Bear Call R1 walls)
            atm_strike = round(spot / 50.0) * 50.0
            has_atm_ce = (atm_strike in chain_by_strike and bool(chain_by_strike[atm_strike].get("CE")))
            has_atm_pe = (atm_strike in chain_by_strike and bool(chain_by_strike[atm_strike].get("PE")))
            strikes_below = [s for s in chain_by_strike if s <= spot and chain_by_strike[s].get("PE")]
            strikes_above = [s for s in chain_by_strike if s >= spot and chain_by_strike[s].get("CE")]
            paired_strikes = [s for s, opts in chain_by_strike.items() if opts.get("CE") and opts.get("PE")]

            if not (has_atm_ce and has_atm_pe and len(strikes_below) >= 2 and len(strikes_above) >= 2 and len(paired_strikes) >= 5):
                incomplete_rejected += 1
                continue

            # 2. Adequate OI Coverage Verification (Tier 2: Do not treat 5 strikes as proof full walls are captured):
            sorted_paired = sorted(paired_strikes)
            min_stk = sorted_paired[0]
            max_stk = sorted_paired[-1]
            span = max_stk - min_stk

            max_pe_stk = max(paired_strikes, key=lambda s: int(chain_by_strike[s]["PE"].get("oi") or 0))
            max_ce_stk = max(paired_strikes, key=lambda s: int(chain_by_strike[s]["CE"].get("oi") or 0))

            is_oi_truncated = (max_pe_stk == min_stk or max_ce_stk == max_stk or span < 300.0)
            if is_oi_truncated:
                oi_truncation_count += 1
            else:
                adequately_covered_count += 1

            # 3. Execution Fillability (Tier 3A: Actual Quotes vs Tier 3B: Estimated Fills):
            has_actual_quotes = (
                chain_by_strike[atm_strike]["CE"]["bid"] > 0 and chain_by_strike[atm_strike]["CE"]["ask"] > 0 and
                chain_by_strike[atm_strike]["PE"]["bid"] > 0 and chain_by_strike[atm_strike]["PE"]["ask"] > 0 and
                any(chain_by_strike[s]["PE"]["bid"] > 0 for s in strikes_below if s < spot) and
                any(chain_by_strike[s]["CE"]["ask"] > 0 for s in strikes_above if s > spot)
            )
            has_estimated_quotes = (
                chain_by_strike[atm_strike]["CE"]["ltp"] > 0 and chain_by_strike[atm_strike]["PE"]["ltp"] > 0 and
                any(chain_by_strike[s]["PE"]["ltp"] > 0 for s in strikes_below if s < spot) and
                any(chain_by_strike[s]["CE"]["ltp"] > 0 for s in strikes_above if s > spot)
            )

            if has_actual_quotes:
                actual_fillable_count += 1
            elif has_estimated_quotes:
                estimated_fillable_count += 1

            snapshots.append({
                "timestamp": ts_str,
                "spot_price": spot,
                "spot_change_pct": spot_chg,
                "expiry": expiry,
                "data_status": "HISTORICAL_REPLAY",
                "is_adequately_covered": not is_oi_truncated,
                "fill_source": "ACTUAL_QUOTES" if has_actual_quotes else ("ESTIMATED_LTP_SPREAD" if has_estimated_quotes else "NONE"),
                "chain_by_strike": chain_by_strike,
            })

        return (
            snapshots,
            incomplete_rejected,
            inconsistent_spot_rejected,
            actual_fillable_count,
            estimated_fillable_count,
            adequately_covered_count,
            oi_truncation_count,
        )
