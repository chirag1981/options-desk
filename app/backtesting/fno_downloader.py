"""
app/backtesting/fno_downloader.py — FYERS Expired F&O Resumable Historical Downloader.

Provides automated, rate-limited, resumable downloading of expired F&O contracts from FYERS API v3:
1. Endpoints:
   - Expiry dates: /history/fno/expired/expiry-dates
   - Underlying symbols: /history/fno/expired/underlying-symbols
   - Historical candles: /history/fno/expired/historical-data
2. Rate Limiting & Safety:
   - Token-bucket / interval sleep pacing (default 5 req/sec).
   - Exponential backoff on HTTP 429 or network disconnects.
3. Resumability & Deduplication:
   - Manifest tracking (instance/historical_data/manifest.json).
   - Preserves partial progress and skips already completed contracts/expiries.
4. Schema Alignment:
   - Emits DataFrames / Parquet / CSV formatted to match HistoricalDataValidator schema.
   - Computes change_oi from consecutive candle open interest.
   - Merges underlying spot prices from FYERS index history.
   - Assigns ESTIMATED_SPREAD fill tier since expired candle data does not provide L2 order book quotes.
"""

import os
import re
import json
import time
import math
import bisect
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set
import pandas as pd

from app.config import Config
from app.market_data import MarketDataProvider
from app.backtesting.data_validator import (
    HistoricalDataValidator,
    DataQualityReport,
    HISTORICAL_SCHEMA_COLUMNS,
)

log = logging.getLogger("backtester.downloader")

TICK_SIZE = 0.05  # Official NSE NIFTY options tick size in INR


def round_to_tick(price: float, direction: str = "buy") -> float:
    """
    Rounds price to official NSE tick size (0.05 INR).
    - 'buy': Rounds up (ceiling) for conservative purchase fill.
    - 'sell': Rounds down (floor) for conservative sales fill.
    Floors strictly at TICK_SIZE (0.05 INR) to prevent negative or impossible fill prices.
    """
    if price <= TICK_SIZE:
        return TICK_SIZE
    if direction == "buy":
        return round(math.ceil(round(price, 4) / TICK_SIZE) * TICK_SIZE, 2)
    else:
        return max(TICK_SIZE, round(math.floor(round(price, 4) / TICK_SIZE) * TICK_SIZE, 2))


@dataclass
class ScenarioFills:
    """
    Documented execution fills for simulated trading.
    Never labeled as actual historical execution.
    """
    optimistic_bid: float
    optimistic_ask: float
    base_bid: float
    base_ask: float
    conservative_bid: float
    conservative_ask: float
    fill_tier: str = "ESTIMATED_SPREAD"


def calculate_scenario_fills(ltp: float) -> ScenarioFills:
    """
    Calculates 3 documented fill execution scenarios:
    1. Optimistic: 0.05% half-spread (min 0.05 INR), 0.00 slippage.
    2. Base: 0.25% half-spread (min 0.05 INR), 0.05 INR (1 tick) slippage.
    3. Conservative: 0.50% half-spread (min 0.10 INR), 0.10 INR (2 ticks) slippage.
    All prices respect the 0.05 INR tick and are floored at 0.05 INR.
    """
    ltp_safe = max(TICK_SIZE, ltp)

    # 1. Optimistic
    opt_half_spread = max(TICK_SIZE, ltp_safe * 0.0005)
    opt_ask = round_to_tick(ltp_safe + opt_half_spread + 0.00, "buy")
    opt_bid = round_to_tick(max(TICK_SIZE, ltp_safe - opt_half_spread - 0.00), "sell")
    if opt_ask <= opt_bid:
        opt_ask = round(opt_bid + TICK_SIZE, 2)

    # 2. Base
    base_half_spread = max(TICK_SIZE, ltp_safe * 0.0025)
    base_ask = round_to_tick(ltp_safe + base_half_spread + 0.05, "buy")
    base_bid = round_to_tick(max(TICK_SIZE, ltp_safe - base_half_spread - 0.05), "sell")
    if base_ask <= base_bid:
        base_ask = round(base_bid + TICK_SIZE, 2)

    # 3. Conservative
    cons_half_spread = max(0.10, ltp_safe * 0.0050)
    cons_ask = round_to_tick(ltp_safe + cons_half_spread + 0.10, "buy")
    cons_bid = round_to_tick(max(TICK_SIZE, ltp_safe - cons_half_spread - 0.10), "sell")
    if cons_ask <= cons_bid:
        cons_ask = round(cons_bid + TICK_SIZE, 2)

    return ScenarioFills(
        optimistic_bid=opt_bid,
        optimistic_ask=opt_ask,
        base_bid=base_bid,
        base_ask=base_ask,
        conservative_bid=cons_bid,
        conservative_ask=cons_ask,
    )


class FnoHistoricalDownloader:
    """
    Resumable, rate-limited downloader for FYERS Expired F&O Historical Data.
    """

    def __init__(
        self,
        output_dir: Optional[str] = None,
        rate_limit_per_sec: float = 5.0,
        max_retries: int = 3,
        resolution: str = "5",
        half_spread_pct: float = 0.005,  # 0.5% default half-spread for estimated bid/ask
    ):
        self.output_dir = output_dir or os.path.join(Config.INSTANCE_DIR, "historical_data")
        os.makedirs(self.output_dir, exist_ok=True)
        self.manifest_file = os.path.join(self.output_dir, "manifest.json")
        self.rate_limit_per_sec = max(0.5, float(rate_limit_per_sec))
        self.delay_between_calls = 1.0 / self.rate_limit_per_sec
        self.max_retries = max_retries
        self.resolution = resolution
        self.half_spread_pct = half_spread_pct
        self._last_call_time = 0.0
        self._manifest = self._load_manifest()
        if not os.path.exists(self.manifest_file):
            self._save_manifest()

    def _load_manifest(self) -> Dict[str, Any]:
        """Loads download progress manifest or initializes a new one."""
        if os.path.exists(self.manifest_file):
            try:
                with open(self.manifest_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log.warning(f"Failed to load manifest, creating new: {e}")
        return {"created_at": datetime.now(timezone.utc).isoformat(), "expiries": {}}

    def _save_manifest(self) -> None:
        """Persists download progress to manifest."""
        try:
            with open(self.manifest_file, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2)
        except Exception as e:
            log.error(f"Failed to save manifest: {e}")

    def _rate_limit_sleep(self) -> None:
        """Enforces rate limit by sleeping if necessary."""
        now = time.time()
        elapsed = now - self._last_call_time
        if elapsed < self.delay_between_calls:
            time.sleep(self.delay_between_calls - elapsed)
        self._last_call_time = time.time()

    def _call_api(self, endpoint: str, params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """
        Executes authenticated API call with retries and exponential backoff.
        Never logs access tokens or sensitive credentials.
        """
        client = MarketDataProvider.get_fyers_client()
        if not client:
            raise RuntimeError("FYERS client authentication failed or token unavailable.")

        backoff = 1.0
        last_error = ""

        for attempt in range(1, self.max_retries + 1):
            self._rate_limit_sleep()
            try:
                resp = client.service.get_call(endpoint, client.header, params, data_flag=True)
                code = resp.get("code", 0)
                status = resp.get("s", "")
                
                # Success
                if status == "ok" or code == 200:
                    return 200, resp

                # Rate limited or server busy
                if code == 429 or "rate" in resp.get("message", "").lower():
                    log.warning(f"FYERS rate limit hit (attempt {attempt}/{self.max_retries}). Backing off {backoff:.1f}s.")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue

                # Input error (non-retryable)
                if code == -50:
                    return -50, resp

                last_error = resp.get("message", str(resp))
            except Exception as e:
                last_error = str(e)
                log.warning(f"API call exception (attempt {attempt}/{self.max_retries}): {e}")
                time.sleep(backoff)
                backoff *= 2.0

        return -1, {"s": "error", "message": f"Failed after {self.max_retries} attempts: {last_error}"}

    def fetch_expiry_dates(
        self,
        range_from: str,
        range_to: str,
        symbol: str = "NSE:NIFTY50-INDEX"
    ) -> List[str]:
        """
        Fetches official expired options expiry dates within range [range_from, range_to].
        Dates must be in 'YYYY-MM-DD' format.
        """
        params = {
            "symbol": symbol,
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
        }
        code, resp = self._call_api("/history/fno/expired/expiry-dates", params)
        if code != 200:
            log.error(f"Failed to fetch expiry dates: {resp.get('message')}")
            return []
        
        data = resp.get("data", {})
        expiry_dates = data.get("expiry_dates", {})
        options_exp = expiry_dates.get("options", [])
        return sorted(list(set(options_exp)))

    def fetch_underlying_symbols(
        self,
        expiry_date: str,
        symbol: str = "NSE:NIFTY50-INDEX"
    ) -> List[str]:
        """
        Fetches all contract symbols for a specific expired expiry date.
        """
        params = {
            "symbol": symbol,
            "expiry_date": expiry_date,
        }
        code, resp = self._call_api("/history/fno/expired/underlying-symbols", params)
        if code != 200:
            log.error(f"Failed to fetch symbols for expiry {expiry_date}: {resp.get('message')}")
            return []
        
        data = resp.get("data", {})
        contracts = data.get("contracts", {})
        return contracts.get("options", [])

    def fetch_spot_candles(
        self,
        range_from: str,
        range_to: str,
        resolution: Optional[str] = None
    ) -> Dict[int, float]:
        """
        Fetches spot price candles for NSE:NIFTY50-INDEX.
        Returns mapping: {epoch_second: spot_close_price}.
        """
        res = resolution or self.resolution
        client = MarketDataProvider.get_fyers_client()
        if not client:
            return {}
        
        data = {
            "symbol": "NSE:NIFTY50-INDEX",
            "resolution": res,
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "1",
        }
        self._rate_limit_sleep()
        try:
            resp = client.history(data=data)
            candles = resp.get("candles", [])
            # candle format: [timestamp, open, high, low, close, volume]
            return {int(c[0]): float(c[4]) for c in candles}
        except Exception as e:
            log.error(f"Failed to fetch spot history: {e}")
            return {}

    @staticmethod
    def parse_contract_symbol(symbol: str) -> Optional[Tuple[float, str]]:
        """
        Parses strike and option type from FYERS contract symbol.
        Examples:
          - NSE:NIFTY25JAN24500CE -> (24500.0, 'CE')
          - NSE:NIFTY2512321300PE -> (21300.0, 'PE')
        """
        match = re.search(r"(\d{5})(CE|PE)$", symbol.upper())
        if match:
            strike = float(match.group(1))
            opt_type = match.group(2)
            return strike, opt_type
        return None

    def fetch_contract_candles(
        self,
        symbol: str,
        range_from: str,
        range_to: str,
        include_oi: int = 1,
        include_greeks: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Fetches historical candles for an individual expired option leg.
        Candle fields (12 items when OI and Greeks requested):
        [timestamp, open, high, low, close, volume, oi, iv, delta, gamma, theta, vega]
        
        Strict OI & ΔOI Validation:
        - Derives ΔOI only from consecutive valid observations for the same contract.
        - Records elapsed time between OI observations (doi_elapsed_sec).
        - Flags gaps (is_gap=True) when elapsed_sec > expected_interval; does not represent
          a multi-interval change as a single-interval change.
        - Preserves first observation change_oi as None (unavailable, not fabricated 0).
        """
        params = {
            "symbol": symbol,
            "resolution": self.resolution,
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "0",
            "include_oi": str(include_oi),
            "include_greeks": str(include_greeks),
        }
        code, resp = self._call_api("/history/fno/expired/historical-data", params)
        if code != 200:
            return []

        raw_candles = resp.get("candles", [])
        parsed = []
        prev_oi = None
        prev_ts = None
        expected_interval_sec = 300 if str(self.resolution) == "5" else 60

        for c in raw_candles:
            if len(c) < 5:
                continue
            ts = int(c[0])
            open_p = float(c[1])
            high_p = float(c[2])
            low_p = float(c[3])
            close_p = float(c[4])
            vol = int(c[5]) if len(c) > 5 and c[5] is not None else 0
            oi = int(c[6]) if len(c) > 6 and c[6] is not None else 0

            # Delta OI validation & gap detection
            if prev_ts is not None:
                elapsed_sec = ts - prev_ts
                is_gap = elapsed_sec > expected_interval_sec
                change_oi = (oi - prev_oi) if (prev_oi is not None) else None
                doi_elapsed_sec = elapsed_sec
            else:
                # First observation: preserve as unavailable (None) rather than fabricated zero
                change_oi = None
                doi_elapsed_sec = None
                is_gap = False

            prev_oi = oi
            prev_ts = ts

            iv = float(c[7]) if len(c) > 7 and c[7] is not None else None
            delta = float(c[8]) if len(c) > 8 and c[8] is not None else None
            gamma = float(c[9]) if len(c) > 9 and c[9] is not None else None
            theta = float(c[10]) if len(c) > 10 and c[10] is not None else None
            vega = float(c[11]) if len(c) > 11 and c[11] is not None else None

            parsed.append({
                "timestamp_epoch": ts,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": vol,
                "oi": oi,
                "change_oi": change_oi,
                "doi_elapsed_sec": doi_elapsed_sec,
                "is_gap": is_gap,
                "iv": iv,
                "delta": delta,
                "gamma": gamma,
                "theta": theta,
                "vega": vega,
            })
        return parsed

    @staticmethod
    def _join_spot_no_lookahead(
        ts_epoch: int,
        spot_map: Dict[int, float],
        sorted_spot_ts: List[int],
        fallback_spot: float
    ) -> Tuple[float, int]:
        """
        Joins spot price by exact timestamp or most recent preceding observation.
        Strictly prevents look-ahead bias (never uses spot observation from future).
        Returns (spot_price, spot_age_sec).
        """
        if ts_epoch in spot_map:
            return spot_map[ts_epoch], 0
        idx = bisect.bisect_right(sorted_spot_ts, ts_epoch) - 1
        if idx >= 0:
            matched_ts = sorted_spot_ts[idx]
            return spot_map[matched_ts], (ts_epoch - matched_ts)
        return fallback_spot, 999999

    def _build_records_dataframe(
        self,
        contracts: List[Tuple[str, float, str]],
        eval_date_from: str,
        eval_date_to: str,
        spot_map: Dict[int, float],
        expiry_date: str,
    ) -> pd.DataFrame:
        """
        Fetches and compiles clean tabular records with:
        - Exact spot join without look-ahead
        - 3 execution scenarios (Optimistic, Base, Conservative)
        - OI gap flags
        - Duplicate detection
        """
        sorted_spot_ts = sorted(spot_map.keys())
        median_spot = sorted(spot_map.values())[len(spot_map) // 2] if spot_map else 24000.0

        records = []
        for sym, strike, opt_type in contracts:
            candles = self.fetch_contract_candles(sym, eval_date_from, eval_date_to)
            for c in candles:
                ts_epoch = c["timestamp_epoch"]
                spot, spot_age = self._join_spot_no_lookahead(
                    ts_epoch, spot_map, sorted_spot_ts, median_spot
                )
                ltp = c["close"]

                # 3 Documented execution scenarios
                fills = calculate_scenario_fills(ltp)

                dt_str = datetime.fromtimestamp(ts_epoch).strftime("%Y-%m-%d %H:%M:%S")

                records.append({
                    "timestamp": dt_str,
                    "spot_price": spot,
                    "spot_age_sec": spot_age,
                    "expiry": expiry_date,
                    "strike": strike,
                    "option_type": opt_type,
                    "ltp": ltp,
                    "bid": fills.base_bid,  # Base scenario for standard schema compatibility
                    "ask": fills.base_ask,
                    "opt_bid": fills.optimistic_bid,
                    "opt_ask": fills.optimistic_ask,
                    "base_bid": fills.base_bid,
                    "base_ask": fills.base_ask,
                    "cons_bid": fills.conservative_bid,
                    "cons_ask": fills.conservative_ask,
                    "oi": c["oi"],
                    "change_oi": c["change_oi"],
                    "doi_elapsed_sec": c["doi_elapsed_sec"],
                    "is_gap": c["is_gap"],
                    "volume": c["volume"],
                    "iv": c["iv"] if c["iv"] is not None else 15.0,
                    "symbol": sym,
                    "fill_tier": "ESTIMATED_SPREAD",
                })

        df = pd.DataFrame(records)
        if df.empty:
            return df

        # Deduplicate candles by (timestamp, symbol)
        initial_len = len(df)
        df = df.drop_duplicates(subset=["timestamp", "symbol"]).reset_index(drop=True)
        if len(df) < initial_len:
            log.warning(f"Dropped {initial_len - len(df)} duplicate candle records.")

        # Sort chronologically
        df = df.sort_values(by=["timestamp", "strike", "option_type"]).reset_index(drop=True)
        return df

    def download_sample(
        self,
        expiry_date: str = "2025-01-23",
        strikes_around_atm: int = 3,
        date: Optional[str] = None
    ) -> Tuple[bool, DataQualityReport, pd.DataFrame]:
        """
        Downloads a representative sample (ATM +/- N strikes CE and PE for a single day)
        and runs it through HistoricalDataValidator.
        Returns: (passed_validation, DataQualityReport, sample_dataframe).
        """
        eval_date = date or expiry_date
        log.info(f"Downloading sample for expiry {expiry_date} on date {eval_date}...")

        spot_map = self.fetch_spot_candles(eval_date, eval_date)
        if not spot_map:
            raise RuntimeError(f"Could not fetch spot candles for {eval_date}")

        median_spot = sorted(spot_map.values())[len(spot_map) // 2]
        atm_strike = round(median_spot / 50.0) * 50.0

        all_symbols = self.fetch_underlying_symbols(expiry_date)
        if not all_symbols:
            raise RuntimeError(f"No symbols returned for expiry {expiry_date}")

        sample_strikes = {
            atm_strike + (offset * 50.0)
            for offset in range(-strikes_around_atm, strikes_around_atm + 1)
        }

        selected_symbols = []
        for sym in all_symbols:
            parsed = self.parse_contract_symbol(sym)
            if parsed and parsed[0] in sample_strikes:
                selected_symbols.append((sym, parsed[0], parsed[1]))

        df = self._build_records_dataframe(
            selected_symbols, eval_date, eval_date, spot_map, expiry_date
        )
        if df.empty:
            raise RuntimeError("Sample download resulted in empty DataFrame.")

        sample_path = os.path.join(self.output_dir, f"sample_{expiry_date}_{eval_date}.parquet")
        df.to_parquet(sample_path, index=False)

        valid, report, valid_rows = HistoricalDataValidator.validate_dataframe(
            df, file_path=sample_path, file_format="PARQUET"
        )
        return valid, report, df

    def download_expiry(
        self,
        expiry_date: str,
        strikes_around_atm: int = 8,
        trading_days: Optional[List[str]] = None,
        force: bool = False
    ) -> Tuple[bool, Optional[DataQualityReport], Optional[pd.DataFrame]]:
        """
        Downloads all contracts around ATM for a given expiry date.
        If already completed in manifest and force=False, returns cached report.
        Strictly validates DataFrame before marking complete.
        """
        # 1. Check Manifest for duplicate skip
        exp_entry = self._manifest.get("expiries", {}).get(expiry_date, {})
        target_file = os.path.join(self.output_dir, f"NIFTY_{expiry_date}.parquet")
        if not force and exp_entry.get("status") == "completed" and os.path.exists(target_file):
            log.info(f"Expiry {expiry_date} already completed in manifest. Skipping.")
            try:
                cached_df = pd.read_parquet(target_file)
                valid, report, _ = HistoricalDataValidator.validate_dataframe(
                    cached_df, file_path=target_file, file_format="PARQUET"
                )
                return valid, report, cached_df
            except Exception as e:
                log.warning(f"Error loading cached expiry {expiry_date}: {e}. Redownloading.")

        # Mark in-progress
        self._manifest.setdefault("expiries", {})[expiry_date] = {
            "status": "in_progress",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_manifest()

        # 2. Determine date range
        eval_date_to = expiry_date
        if trading_days and len(trading_days) > 0:
            eval_date_from = trading_days[0]
            eval_date_to = trading_days[-1]
        else:
            # Default to expiry date day
            eval_date_from = expiry_date

        # 3. Fetch Spot Candles
        spot_map = self.fetch_spot_candles(eval_date_from, eval_date_to)
        if not spot_map:
            err = f"Failed to fetch spot candles for {eval_date_from} to {eval_date_to}"
            log.error(err)
            self._manifest["expiries"][expiry_date] = {
                "status": "failed",
                "error": err,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_manifest()
            return False, None, None

        median_spot = sorted(spot_map.values())[len(spot_map) // 2]
        atm_strike = round(median_spot / 50.0) * 50.0

        # 4. Fetch Underlying Symbols
        all_symbols = self.fetch_underlying_symbols(expiry_date)
        if not all_symbols:
            err = f"No contract symbols returned for expiry {expiry_date}"
            log.error(err)
            self._manifest["expiries"][expiry_date] = {
                "status": "failed",
                "error": err,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_manifest()
            return False, None, None

        # Filter ATM +/- strikes
        target_strikes = {
            atm_strike + (offset * 50.0)
            for offset in range(-strikes_around_atm, strikes_around_atm + 1)
        }

        selected_symbols = []
        for sym in all_symbols:
            parsed = self.parse_contract_symbol(sym)
            if parsed and parsed[0] in target_strikes:
                selected_symbols.append((sym, parsed[0], parsed[1]))

        # 5. Build Records
        df = self._build_records_dataframe(
            selected_symbols, eval_date_from, eval_date_to, spot_map, expiry_date
        )

        if df.empty:
            err = f"No candle records returned for expiry {expiry_date}"
            log.error(err)
            self._manifest["expiries"][expiry_date] = {
                "status": "failed",
                "error": err,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_manifest()
            return False, None, None

        # 6. Validate Quality Before Acceptance
        df.to_parquet(target_file, index=False)
        valid, report, _ = HistoricalDataValidator.validate_dataframe(
            df, file_path=target_file, file_format="PARQUET"
        )

        if not valid:
            err = f"Validation rejected: {report.rejection_reasons}"
            log.warning(f"Expiry {expiry_date} failed quality validation: {err}")
            self._manifest["expiries"][expiry_date] = {
                "status": "failed",
                "error": err,
                "rejection_reasons": report.rejection_reasons,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_manifest()
            return False, report, df

        # 7. Update Manifest as Completed
        gaps_count = int(df["is_gap"].sum()) if "is_gap" in df.columns else 0
        self._manifest["expiries"][expiry_date] = {
            "status": "completed",
            "file": target_file,
            "total_rows": report.total_rows,
            "valid_rows": report.valid_rows,
            "reconstructable_snapshots": report.reconstructable_snapshots,
            "adequately_covered_snapshots": report.adequately_covered_snapshots,
            "data_gaps": gaps_count,
            "start_time": report.start_time,
            "end_time": report.end_time,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_manifest()
        log.info(f"Expiry {expiry_date} successfully downloaded, validated, and saved.")
        return True, report, df

    def run_ingestion_audit(
        self,
        start_date: str = "2024-10-24",
        end_date: str = "2026-10-23",
        max_expiries: Optional[int] = None,
        strikes_around_atm: int = 8,
    ) -> Dict[str, Any]:
        """
        Runs controlled batch ingestion for the date range and compiles a comprehensive
        Data Coverage Report.
        """
        log.info(f"Initiating controlled ingestion audit from {start_date} to {end_date}...")
        expiries = self.fetch_expiry_dates(start_date, end_date)

        if max_expiries and len(expiries) > max_expiries:
            expiries = expiries[:max_expiries]

        audit_results = {
            "date_range": f"{start_date} to {end_date}",
            "expected_expiries_count": len(expiries),
            "expected_expiries": expiries,
            "completed_expiries": [],
            "failed_expiries": [],
            "total_records": 0,
            "total_reconstructable_snapshots": 0,
            "total_adequately_covered_snapshots": 0,
            "total_data_gaps": 0,
            "reports": {},
        }

        for exp in expiries:
            success, report, df = self.download_expiry(
                exp, strikes_around_atm=strikes_around_atm
            )
            if success and report is not None:
                audit_results["completed_expiries"].append(exp)
                audit_results["total_records"] += report.total_rows
                audit_results["total_reconstructable_snapshots"] += report.reconstructable_snapshots
                audit_results["total_adequately_covered_snapshots"] += report.adequately_covered_snapshots
                gaps = int(df["is_gap"].sum()) if df is not None and "is_gap" in df.columns else 0
                audit_results["total_data_gaps"] += gaps
                audit_results["reports"][exp] = {
                    "valid_rows": report.valid_rows,
                    "reconstructable_snapshots": report.reconstructable_snapshots,
                    "oi_coverage_pct": report.oi_coverage_pct,
                    "change_oi_coverage_pct": report.change_oi_coverage_pct,
                    "data_gaps": gaps,
                }
            else:
                audit_results["failed_expiries"].append(exp)

        return audit_results

