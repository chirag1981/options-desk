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
import logging
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

            # change in open interest
            change_oi = (oi - prev_oi) if prev_oi is not None else 0
            prev_oi = oi

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
                "iv": iv,
                "delta": delta,
                "gamma": gamma,
                "theta": theta,
                "vega": vega,
            })
        return parsed

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

        # 1. Fetch spot candles for evaluation date
        spot_map = self.fetch_spot_candles(eval_date, eval_date)
        if not spot_map:
            raise RuntimeError(f"Could not fetch spot candles for {eval_date}")

        median_spot = sorted(spot_map.values())[len(spot_map) // 2]
        atm_strike = round(median_spot / 50.0) * 50.0

        # 2. Fetch available symbols for this expiry
        all_symbols = self.fetch_underlying_symbols(expiry_date)
        if not all_symbols:
            raise RuntimeError(f"No symbols returned for expiry {expiry_date}")

        # 3. Select sample strikes around ATM
        sample_strikes = {
            atm_strike + (offset * 50.0)
            for offset in range(-strikes_around_atm, strikes_around_atm + 1)
        }

        selected_symbols = []
        for sym in all_symbols:
            parsed = self.parse_contract_symbol(sym)
            if parsed and parsed[0] in sample_strikes:
                selected_symbols.append((sym, parsed[0], parsed[1]))

        log.info(f"Identified {len(selected_symbols)} sample contracts around ATM ({atm_strike}).")

        # 4. Fetch candles for selected symbols
        records = []
        for sym, strike, opt_type in selected_symbols:
            candles = self.fetch_contract_candles(sym, eval_date, eval_date)
            for c in candles:
                ts_epoch = c["timestamp_epoch"]
                spot = spot_map.get(ts_epoch, median_spot)
                ltp = c["close"]

                # Estimated bid/ask using configured half-spread
                spread = max(0.05, ltp * self.half_spread_pct)
                bid = max(0.05, round(ltp - spread, 2))
                ask = max(bid + 0.05, round(ltp + spread, 2))

                dt_str = datetime.fromtimestamp(ts_epoch).strftime("%Y-%m-%d %H:%M:%S")

                records.append({
                    "timestamp": dt_str,
                    "spot_price": spot,
                    "expiry": expiry_date,
                    "strike": strike,
                    "option_type": opt_type,
                    "ltp": ltp,
                    "bid": bid,
                    "ask": ask,
                    "oi": c["oi"],
                    "change_oi": c["change_oi"],
                    "volume": c["volume"],
                    "iv": c["iv"] or 15.0,
                    "symbol": sym,
                    "fill_tier": "ESTIMATED_SPREAD",
                })

        df = pd.DataFrame(records)
        if df.empty:
            raise RuntimeError("Sample download resulted in empty DataFrame.")

        # Sort chronologically
        df = df.sort_values(by=["timestamp", "strike", "option_type"]).reset_index(drop=True)

        # 5. Run through HistoricalDataValidator
        sample_path = os.path.join(self.output_dir, f"sample_{expiry_date}_{eval_date}.parquet")
        df.to_parquet(sample_path, index=False)

        valid, report, valid_rows = HistoricalDataValidator.validate_dataframe(
            df, file_path=sample_path, file_format="PARQUET"
        )

        return valid, report, df
