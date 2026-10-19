"""
app/services/fyers_options_service.py — FYERS Native Option Chain & Real-Time Market Data Provider
Fetches complete, multi-strike Option Chains with Live OI, Change in OI, Greeks, and Spot Prices
in a single sub-second API call via FYERS API v3.
"""

import os
import json
import math
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from app.services.fyers_auth import (
    get_fyers_index_spot,
    get_fyers_index_ohlc_details,
    get_fyers_model,
    invalidate_cached_token,
)

log = logging.getLogger("fyers_options_service")
IST = ZoneInfo("Asia/Kolkata")

# Supported Index Configurations
INDEX_CONFIGS = {
    "NIFTY": {
        "token_symbol": "NIFTY",
        "fyers_symbol": "NSE:NIFTY50-INDEX",
        "name": "NIFTY 50",
        "lot_size": 65,
        "strike_step": 50,
        "default_spot": 22500.0,
        "exchange": "NSE",
        "exch_code": "NSE",
    },
}


_OPTION_CHAIN_CACHE = {}
_EXPIRIES_CACHE = {}
_CACHE_TTL_SECONDS = 60  # 1 minute fresh cache
_OI_BASELINE_LOCK = threading.Lock()
_OI_BASELINE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../instance/oi_baseline.json"))
_OI_BASELINE: dict = {"date": None, "oi": {}}


def _change_in_oi(quote: dict | None) -> int:
    """
    Change in OI for a quote: oi - previous-day OI when the broker supplies 'poi' or 'oich',
    otherwise oi - first OI observed today for that contract (0 on first sighting).
    """
    if not quote:
        return 0
    if "oich" in quote and quote.get("oich") is not None:
        return int(quote["oich"])
    oi = int(quote.get("oi", 0) or 0)
    poi = quote.get("poi")
    if poi not in (None, ""):
        return oi - int(float(poi))

    tsym = quote.get("tsym") or quote.get("symbol")
    if not tsym or oi <= 0:
        return 0

    today = datetime.now(IST).strftime("%Y-%m-%d")
    with _OI_BASELINE_LOCK:
        if _OI_BASELINE["date"] != today:
            loaded = {}
            try:
                with open(_OI_BASELINE_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if saved.get("date") == today:
                    loaded = saved.get("oi", {})
            except Exception:
                pass
            _OI_BASELINE["date"] = today
            _OI_BASELINE["oi"] = loaded

        base = _OI_BASELINE["oi"].get(tsym)
        if base is None:
            _OI_BASELINE["oi"][tsym] = oi
            try:
                os.makedirs(os.path.dirname(_OI_BASELINE_PATH), exist_ok=True)
                with open(_OI_BASELINE_PATH, "w", encoding="utf-8") as f:
                    json.dump(_OI_BASELINE, f)
            except Exception:
                pass
            return 0
    return oi - int(base)


def calculate_implied_volatility(
    price: float, spot: float, strike: float, t: float, option_type: str = "CE", r: float = 0.065
) -> float | None:
    """
    Solves Black-Scholes implied volatility for given option price, spot, strike, and time to maturity.
    Returns annual IV percentage (e.g. 14.50) or None if unsolvable/illiquid.
    """
    if price <= 0.05 or spot <= 0 or strike <= 0 or t <= 0:
        return None

    intrinsic = max(0.0, spot - strike * math.exp(-r * t)) if option_type == "CE" else max(0.0, strike * math.exp(-r * t) - spot)
    if price < intrinsic:
        return None

    def norm_cdf(x):
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

    low_vol = 0.01   # 1%
    high_vol = 3.00  # 300%

    def bs_p(sigma):
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        d2 = d1 - sigma * math.sqrt(t)
        if option_type == "CE":
            return spot * norm_cdf(d1) - strike * math.exp(-r * t) * norm_cdf(d2)
        else:
            return strike * math.exp(-r * t) * norm_cdf(-d2) - spot * norm_cdf(-d1)

    for _ in range(30):
        mid_vol = (low_vol + high_vol) / 2.0
        try:
            val = bs_p(mid_vol)
        except Exception:
            return None
        diff = val - price
        if abs(diff) < 0.05:
            iv_pct = round(mid_vol * 100.0, 2)
            return iv_pct if 1.0 <= iv_pct <= 200.0 else None
        if diff < 0:
            low_vol = mid_vol
        else:
            high_vol = mid_vol

    iv_pct = round(mid_vol * 100.0, 2)
    return iv_pct if 1.0 <= iv_pct <= 200.0 else None


def calculate_delta(
    spot: float, strike: float, t: float, iv_pct: float | None, option_type: str = "CE", r: float = 0.065
) -> float | None:
    """Calculates Black-Scholes Option Delta given spot, strike, DTE, and IV."""
    if not iv_pct or iv_pct <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return None
    try:
        sigma = iv_pct / 100.0
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        norm_d1 = (1.0 + math.erf(d1 / math.sqrt(2.0))) / 2.0
        if option_type == "CE":
            return round(norm_d1, 2)
        else:
            return round(norm_d1 - 1.0, 2)
    except Exception:
        return None


def get_supported_indices() -> list[dict]:
    """Returns list of supported underlying index symbols and metadata."""
    return [
        {
            "symbol": sym,
            "name": cfg["name"],
            "lot_size": cfg["lot_size"],
            "strike_step": cfg["strike_step"],
            "exchange": cfg["exchange"]
        }
        for sym, cfg in INDEX_CONFIGS.items()
    ]


def get_upcoming_expiries(symbol: str = "NIFTY") -> list[str]:
    """
    Fetches real active upcoming expiries directly from FYERS option chain metadata,
    sorted chronologically.
    """
    sym = (symbol or "NIFTY").upper()
    if sym not in INDEX_CONFIGS:
        sym = "NIFTY"

    cfg = INDEX_CONFIGS[sym]
    now = datetime.now(IST)
    cache_entry = _EXPIRIES_CACHE.get(sym)
    if cache_entry and (now - cache_entry["fetch_time"]).total_seconds() < 1800:
        return cache_entry["expiries"]

    fyers = get_fyers_model()
    if fyers:
        try:
            oc_resp = fyers.optionchain(data={"symbol": cfg["fyers_symbol"], "strikecount": 2})
            if oc_resp.get("s") == "ok":
                exp_list = oc_resp.get("data", {}).get("expiryData", [])
                date_strings = []
                today_d = now.date()
                for item in exp_list:
                    raw_date = item.get("date", "")
                    try:
                        # FYERS format is DD-MM-YYYY (e.g. 06-10-2026) -> convert to DD-MMM-YYYY (06-OCT-2026)
                        dt = datetime.strptime(raw_date, "%d-%m-%Y").date()
                        if dt >= today_d:
                            formatted = dt.strftime("%d-%b-%Y").upper()
                            date_strings.append((dt, formatted, item.get("expiry")))
                    except Exception:
                        pass

                date_strings.sort(key=lambda x: x[0])
                if date_strings:
                    sorted_expiries = [x[1] for x in date_strings[:8]]
                    exp_ts_map = {x[1]: x[2] for x in date_strings}
                    _EXPIRIES_CACHE[sym] = {
                        "expiries": sorted_expiries,
                        "ts_map": exp_ts_map,
                        "fetch_time": now
                    }
                    return sorted_expiries
            elif oc_resp.get("code") in (-16, 401) or "invalid or expired" in str(oc_resp.get("message", "")).lower():
                log.warning(f"FYERS expiries fetch token expired/invalid (code {oc_resp.get('code')}); invalidating cached token.")
                invalidate_cached_token()
        except Exception as e:
            log.warning(f"Failed to fetch expiries from FYERS for {sym}: {e}")

    # Fallback schedules
    FALLBACK_SCHEDULES = {
        "NIFTY": ["06-OCT-2026", "13-OCT-2026", "19-OCT-2026", "27-OCT-2026"],
    }
    return FALLBACK_SCHEDULES.get(sym, ["06-OCT-2026", "13-OCT-2026", "19-OCT-2026", "27-OCT-2026"])


def get_live_spot_price(symbol: str = "NIFTY") -> tuple[float, float, str]:
    """
    Fetches real-time spot price and day change for index from FYERS.
    Returns (spot_price, change_pct, status).
    """
    cfg = INDEX_CONFIGS.get(symbol.upper(), INDEX_CONFIGS["NIFTY"])
    default_price = cfg["default_spot"]

    try:
        spot = get_fyers_index_spot(symbol)
        if spot and spot[0] > 0:
            return spot
    except Exception as e:
        log.warning(f"FYERS spot quote error for {symbol}: {e}")

    return default_price, 0.0, "DATA_UNAVAILABLE"


_CANDLE_CACHE = {}
_CANDLE_CACHE_TTL_SEC = 30


def get_underlying_candles(
    symbol: str = "NIFTY",
    resolution: str = "5",
    limit: int = 30,
    force_refresh: bool = False,
) -> list[dict]:
    """
    Fetches real completed and current intraday OHLC candles for the underlying index via FYERS history API.
    Returns a list of structured candle dicts:
    [
        {
            "timestamp": epoch_start,
            "datetime": "YYYY-MM-DD HH:MM:SS",
            "open": float,
            "high": float,
            "low": float,
            "close": float,
            "volume": float,
            "is_completed": bool,
        },
        ...
    ]
    """
    sym = (symbol or "NIFTY").upper()
    cfg = INDEX_CONFIGS.get(sym, INDEX_CONFIGS.get("NIFTY"))
    fy_sym = cfg.get("fyers_symbol", "NSE:NIFTY50-INDEX")

    now = datetime.now(IST)
    cache_key = (sym, str(resolution))
    if not force_refresh and cache_key in _CANDLE_CACHE:
        cached = _CANDLE_CACHE[cache_key]
        if (now - cached["fetch_time"]).total_seconds() < _CANDLE_CACHE_TTL_SEC:
            return cached["candles"]

    fyers = get_fyers_model()
    if not fyers:
        return []

    try:
        try:
            res_minutes = int(resolution)
        except (TypeError, ValueError):
            res_minutes = 5
        res_seconds = res_minutes * 60

        today_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
        from_epoch = int(today_start.timestamp())
        to_epoch = int(now.timestamp())

        if to_epoch - from_epoch < 3600:
            from_epoch -= 86400 * 2

        data_req = {
            "symbol": fy_sym,
            "resolution": str(resolution),
            "date_format": "0",
            "range_from": str(from_epoch),
            "range_to": str(to_epoch),
            "cont_flag": "1",
        }

        resp = fyers.history(data=data_req)
        if resp.get("s") == "ok":
            raw_candles = resp.get("candles", [])
            candles_out = []
            now_epoch = int(now.timestamp())

            for c in raw_candles:
                if len(c) >= 5:
                    c_epoch = int(c[0])
                    c_dt = datetime.fromtimestamp(c_epoch, tz=IST).strftime("%Y-%m-%d %H:%M:%S")
                    is_done = (now_epoch >= c_epoch + res_seconds)
                    candles_out.append({
                        "timestamp": c_epoch,
                        "datetime": c_dt,
                        "open": round(float(c[1]), 2),
                        "high": round(float(c[2]), 2),
                        "low": round(float(c[3]), 2),
                        "close": round(float(c[4]), 2),
                        "volume": round(float(c[5]), 2) if len(c) > 5 else 0.0,
                        "is_completed": is_done,
                    })

            if len(candles_out) > limit:
                candles_out = candles_out[-limit:]

            _CANDLE_CACHE[cache_key] = {
                "candles": candles_out,
                "fetch_time": now,
            }
            return candles_out
        elif resp.get("code") in (-16, 401) or "invalid or expired" in str(resp.get("message", "")).lower():
            log.warning("FYERS token expired during history candle fetch; invalidating.")
            invalidate_cached_token()
    except Exception as e:
        log.warning(f"Error fetching FYERS underlying candles for {sym}: {e}")

    return []


def fetch_option_chain_data(
    symbol: str = "NIFTY",
    expiry: str | None = None,
    force_refresh: bool = False,
    allow_simulation: bool = False,
) -> dict:
    """
    Fetches complete Option Chain data backed by FYERS API v3 optionchain in a single fast call.
    If real data is unavailable, marks data_status = 'DATA_UNAVAILABLE' and fails closed.
    """
    symbol = (symbol or "NIFTY").upper()
    if symbol not in INDEX_CONFIGS:
        symbol = "NIFTY"

    cfg = INDEX_CONFIGS[symbol]
    expiries = get_upcoming_expiries(symbol)
    selected_expiry = expiry if expiry and expiry in expiries else (expiries[0] if expiries else "06-OCT-2026")

    cache_key = (symbol, selected_expiry, allow_simulation)
    now = datetime.now(IST)

    # Check cache
    if not force_refresh and cache_key in _OPTION_CHAIN_CACHE:
        cached = _OPTION_CHAIN_CACHE[cache_key]
        age_seconds = (now - cached["fetch_time"]).total_seconds()
        ttl = _CACHE_TTL_SECONDS if cached.get("status") == "LIVE" else 5
        if age_seconds < ttl:
            return cached["data"]

    fyers = get_fyers_model()
    chain_records = []
    spot_price = cfg["default_spot"]
    spot_chg = 0.0
    data_status = "DATA_UNAVAILABLE"
    source = "FYERS_UNAVAILABLE"

    # Compute DTE in years
    t_years = 4.0 / 365.0
    if selected_expiry:
        try:
            exp_dt = datetime.strptime(selected_expiry, "%d-%b-%Y").date()
            today_d = now.date()
            diff_d = float((exp_dt - today_d).days)
            if diff_d <= 0:
                now_mins = now.hour * 60 + now.minute
                market_close_mins = 15 * 60 + 30
                mins_left = max(1.0, float(market_close_mins - now_mins))
                t_years = mins_left / (365.0 * 24.0 * 60.0)
            else:
                t_years = max(diff_d, 0.0) / 365.0
        except Exception:
            t_years = 4.0 / 365.0

    if fyers:
        try:
            # Check if we have a specific expiry timestamp in cache
            exp_ts = None
            if sym_cache := _EXPIRIES_CACHE.get(symbol):
                exp_ts = sym_cache.get("ts_map", {}).get(selected_expiry)

            req_payload = {
                "symbol": cfg["fyers_symbol"],
                "strikecount": 10,
            }
            if exp_ts:
                req_payload["timestamp"] = exp_ts

            oc_resp = fyers.optionchain(data=req_payload)
            if oc_resp.get("s") == "ok":
                oc_data = oc_resp.get("data", {})
                raw_chain = oc_data.get("optionsChain", [])

                # Extract spot price from the first index entry or separate spot quote
                for item in raw_chain:
                    if item.get("strike_price") == -1:
                        spot_price = float(item.get("ltp", spot_price) or spot_price)
                        spot_chg = float(item.get("ltpchp", 0.0) or 0.0)
                        break

                # Group by strike
                strikes_map = {}
                for item in raw_chain:
                    strike = item.get("strike_price")
                    if strike is None or strike < 0:
                        continue
                    strike_f = float(strike)
                    optt = (item.get("option_type") or "").upper()
                    if strike_f not in strikes_map:
                        strikes_map[strike_f] = {"strike": strike_f, "CE": {}, "PE": {}}
                    if optt in ("CE", "PE"):
                        strikes_map[strike_f][optt] = item

                for strike_f, s_data in sorted(strikes_map.items()):
                    ce = s_data.get("CE", {})
                    pe = s_data.get("PE", {})

                    ce_ltp = float(ce.get("ltp", 0.0) or 0.0)
                    ce_bid = float(ce.get("bid", 0.0) or 0.0)
                    ce_ask = float(ce.get("ask", 0.0) or 0.0)
                    ce_oi = int(ce.get("oi", 0) or 0)
                    ce_chg_oi = int(ce.get("oich", 0) or 0)
                    ce_vol = int(ce.get("volume", 0) or 0)

                    pe_ltp = float(pe.get("ltp", 0.0) or 0.0)
                    pe_bid = float(pe.get("bid", 0.0) or 0.0)
                    pe_ask = float(pe.get("ask", 0.0) or 0.0)
                    pe_oi = int(pe.get("oi", 0) or 0)
                    pe_chg_oi = int(pe.get("oich", 0) or 0)
                    pe_vol = int(pe.get("volume", 0) or 0)

                    ce_chp = float(ce.get("ltpchp", 0.0) or 0.0)
                    pe_chp = float(pe.get("ltpchp", 0.0) or 0.0)
                    ce_sym = ce.get("symbol")
                    pe_sym = pe.get("symbol")

                    # Compute IV & Delta
                    ce_calc_price = (ce_bid + ce_ask) / 2.0 if (ce_bid > 0 and ce_ask > 0) else ce_ltp
                    pe_calc_price = (pe_bid + pe_ask) / 2.0 if (pe_bid > 0 and pe_ask > 0) else pe_ltp

                    ce_iv = calculate_implied_volatility(ce_calc_price, spot_price, strike_f, t_years, "CE") if (t_years and ce_calc_price > 0.05) else None
                    ce_delta = calculate_delta(spot_price, strike_f, t_years, ce_iv, "CE") if (t_years and ce_iv) else None

                    pe_iv = calculate_implied_volatility(pe_calc_price, spot_price, strike_f, t_years, "PE") if (t_years and pe_calc_price > 0.05) else None
                    pe_delta = calculate_delta(spot_price, strike_f, t_years, pe_iv, "PE") if (t_years and pe_iv) else None

                    chain_records.append({
                        "strike": strike_f,
                        "ce_ltp": round(ce_ltp, 2),
                        "ce_bid": round(ce_bid, 2) if ce_bid > 0 else None,
                        "ce_ask": round(ce_ask, 2) if ce_ask > 0 else None,
                        "ce_oi": ce_oi,
                        "ce_change_oi": ce_chg_oi,
                        "ce_volume": ce_vol,
                        "ce_iv": ce_iv,
                        "ce_delta": ce_delta,
                        "ce_symbol": ce_sym,
                        "ce_chp": ce_chp,
                        "pe_ltp": round(pe_ltp, 2),
                        "pe_bid": round(pe_bid, 2) if pe_bid > 0 else None,
                        "pe_ask": round(pe_ask, 2) if pe_ask > 0 else None,
                        "pe_oi": pe_oi,
                        "pe_change_oi": pe_chg_oi,
                        "pe_volume": pe_vol,
                        "pe_iv": pe_iv,
                        "pe_delta": pe_delta,
                        "pe_symbol": pe_sym,
                        "pe_chp": pe_chp,
                    })

                if chain_records:
                    data_status = "LIVE"
                    source = "FYERS_LIVE"
            elif oc_resp.get("code") in (-16, 401) or "invalid or expired" in str(oc_resp.get("message", "")).lower():
                log.warning(f"FYERS optionchain token expired/invalid (code {oc_resp.get('code')}); invalidating cached token.")
                invalidate_cached_token()

        except Exception as e:
            log.warning(f"Error fetching FYERS option chain for {symbol}: {e}")

    # Fallback to simulation if requested and broker unavailable
    if not chain_records:
        if allow_simulation:
            chain_records = _generate_option_chain_records(symbol, cfg, spot_price, selected_expiry)
            data_status = "SIMULATED"
            source = "SIMULATED_TEST"
        else:
            data_status = "DATA_UNAVAILABLE"
            source = "FYERS_UNAVAILABLE"

    # Fetch full OHLC quote details if available
    ohlc = None
    try:
        ohlc = get_fyers_index_ohlc_details(symbol)
    except Exception:
        pass

    open_price = ohlc.get("open_price", spot_price) if ohlc else spot_price
    high_price = ohlc.get("high_price", spot_price) if ohlc else spot_price
    low_price = ohlc.get("low_price", spot_price) if ohlc else spot_price
    prev_close = ohlc.get("prev_close", round(spot_price / (1.0 + spot_chg / 100.0), 2) if spot_chg != -100 else spot_price) if ohlc else round(spot_price / (1.0 + spot_chg / 100.0), 2)

    atm_strike = round(spot_price / cfg["strike_step"]) * cfg["strike_step"]

    result = {
        "symbol": symbol,
        "name": cfg["name"],
        "spot_price": round(spot_price, 2),
        "spot_change_pct": round(spot_chg, 2),
        "open_price": round(open_price, 2),
        "high_price": round(high_price, 2),
        "low_price": round(low_price, 2),
        "prev_close": round(prev_close, 2),
        "atm_strike": atm_strike,
        "strike_step": cfg["strike_step"],
        "lot_size": cfg["lot_size"],
        "expiry": selected_expiry,
        "available_expiries": expiries,
        "data_status": data_status,
        "data_source": source,
        "last_updated": now.strftime("%H:%M:%S"),
        "timestamp_iso": now.isoformat(),
        "chain": chain_records,
        "underlying_candles": get_underlying_candles(symbol=symbol, resolution="5", limit=30),
    }

    # Cache result
    _OPTION_CHAIN_CACHE[cache_key] = {
        "data": result,
        "fetch_time": now,
        "status": data_status
    }

    return result


def _generate_option_chain_records(symbol: str, cfg: dict, spot: float, expiry: str) -> list[dict]:
    """Generates synthetic option chain records for offline tests / simulations."""
    step = cfg["strike_step"]
    atm = round(spot / step) * step
    strikes = [atm + i * step for i in range(-10, 11)]
    chain = []
    base_iv = 14.5
    r = 0.065
    t = 4.0 / 365.0

    for strike in strikes:
        d1 = (math.log(spot / strike) + (r + 0.5 * (base_iv/100)**2) * t) / ((base_iv/100) * math.sqrt(t))
        d2 = d1 - (base_iv/100) * math.sqrt(t)

        def norm_cdf(x):
            return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

        ce_price = max(spot * norm_cdf(d1) - strike * math.exp(-r * t) * norm_cdf(d2), 0.05)
        pe_price = max(strike * math.exp(-r * t) * norm_cdf(-d2) - spot * norm_cdf(-d1), 0.05)

        chain.append({
            "strike": strike,
            "ce_ltp": round(ce_price, 2),
            "ce_bid": round(ce_price * 0.995, 2),
            "ce_ask": round(ce_price * 1.005, 2),
            "ce_oi": 1500000,
            "ce_change_oi": 50000,
            "ce_volume": 120000,
            "ce_iv": base_iv,
            "ce_delta": round(norm_cdf(d1), 2),
            "pe_ltp": round(pe_price, 2),
            "pe_bid": round(pe_price * 0.995, 2),
            "pe_ask": round(pe_price * 1.005, 2),
            "pe_oi": 1600000,
            "pe_change_oi": -40000,
            "pe_volume": 110000,
            "pe_iv": base_iv,
            "pe_delta": round(norm_cdf(d1) - 1.0, 2),
            "ce_symbol": None,
            "ce_chp": 0.0,
            "pe_symbol": None,
            "pe_chp": 0.0,
        })
    return chain


_OPTION_STATS_CACHE: dict[str, dict] = {}
_OPTION_STATS_LOCK = threading.Lock()

def get_option_contract_stats(fyers_symbol: str) -> dict | None:
    """
    Fetches real-time intraday quote statistics (high_price, low_price, open_price, chp)
    from FYERS for an option contract. Cached for 30s to minimize API overhead.
    """
    if not fyers_symbol:
        return None

    now = datetime.now(IST)
    with _OPTION_STATS_LOCK:
        if cached := _OPTION_STATS_CACHE.get(fyers_symbol):
            if (now - cached["time"]).total_seconds() < 30.0:
                return cached["data"]

    fyers = get_fyers_model()
    if not fyers:
        return None

    try:
        res = fyers.quotes(data={"symbols": fyers_symbol})
        items = res.get("d", [])
        if items:
            v = items[0].get("v", {})
            stats = {
                "high_price": float(v.get("high_price", 0.0) or 0.0),
                "low_price": float(v.get("low_price", 0.0) or 0.0),
                "open_price": float(v.get("open_price", 0.0) or 0.0),
                "prev_close": float(v.get("prev_close_price", 0.0) or 0.0),
                "chp": float(v.get("chp", 0.0) or 0.0),
                "lp": float(v.get("lp", 0.0) or 0.0),
            }
            with _OPTION_STATS_LOCK:
                _OPTION_STATS_CACHE[fyers_symbol] = {"time": now, "data": stats}
            return stats
    except Exception as e:
        log.warning(f"Error fetching stats for option {fyers_symbol}: {e}")
    return None

