"""
app/services/fyers_options_service.py — Fyers Option Chain & Market Data Provider
Fetches live Option Chains, Spot Prices, and Expiries from Fyers API v3 with intelligent caching
and realistic off-market / resilient market fallback simulation.
"""

import math
import logging
from datetime import datetime, timedelta, date
from app.services.fyers_auth import get_fyers_client

log = logging.getLogger("fyers_options_service")

# Supported Index Configurations
INDEX_CONFIGS = {
    "NIFTY": {
        "fyers_symbol": "NSE:NIFTY50-INDEX",
        "name": "NIFTY 50",
        "lot_size": 25,
        "strike_step": 50,
        "default_spot": 22500.0,
        "exchange": "NSE",
        "opt_prefix": "NSE:NIFTY",
    },
    "BANKNIFTY": {
        "fyers_symbol": "NSE:NIFTYBANK-INDEX",
        "name": "BANK NIFTY",
        "lot_size": 15,
        "strike_step": 100,
        "default_spot": 54500.0,
        "exchange": "NSE",
        "opt_prefix": "NSE:BANKNIFTY",
    },
    "FINNIFTY": {
        "fyers_symbol": "NSE:FINNIFTY-INDEX",
        "name": "FIN NIFTY",
        "lot_size": 25,
        "strike_step": 50,
        "default_spot": 24600.0,
        "exchange": "NSE",
        "opt_prefix": "NSE:FINNIFTY",
    },
    "MIDCPNIFTY": {
        "fyers_symbol": "NSE:MIDCPNIFTY-INDEX",
        "name": "MIDCP NIFTY",
        "lot_size": 50,
        "strike_step": 25,
        "default_spot": 13700.0,
        "exchange": "NSE",
        "opt_prefix": "NSE:MIDCPNIFTY",
    },
    "SENSEX": {
        "fyers_symbol": "BSE:SENSEX-INDEX",
        "name": "BSE SENSEX",
        "lot_size": 10,
        "strike_step": 100,
        "default_spot": 72200.0,
        "exchange": "BSE",
        "opt_prefix": "BSE:SENSEX",
    },
};

# In-memory option chain cache: {(symbol, expiry): {"data": ..., "timestamp": ..., "status": ...}}
_OPTION_CHAIN_CACHE = {}
_CACHE_TTL_SECONDS = 180  # 3 minutes


def get_supported_indices() -> list[dict]:
    """Returns list of supported underlying index symbols and metadata."""
    return [
        {
            "symbol": sym,
            "name": cfg["name"],
            "fyers_symbol": cfg["fyers_symbol"],
            "lot_size": cfg["lot_size"],
            "strike_step": cfg["strike_step"],
            "exchange": cfg["exchange"]
        }
        for sym, cfg in INDEX_CONFIGS.items()
    ]


def get_live_spot_price(symbol: str = "NIFTY") -> tuple[float, float, str]:
    """
    Fetches live spot price and day change for index from Fyers API.
    Returns (spot_price, change_pct, status).
    """
    cfg = INDEX_CONFIGS.get(symbol.upper(), INDEX_CONFIGS["NIFTY"])
    fyers_sym = cfg["fyers_symbol"]
    default_price = cfg["default_spot"]

    client = get_fyers_client()
    if client:
        try:
            resp = client.quotes(data={"symbols": fyers_sym})
            if resp and resp.get("s") == "ok" and resp.get("d"):
                v = resp["d"][0].get("v", {})
                cmd = v.get("cmd", {})
                ltp = float(cmd.get("c", 0.0) or v.get("lp", 0.0) or 0.0)
                prev_close = float(v.get("prev_close_price", 0.0) or ltp)
                chg_pct = float(v.get("chp", 0.0) or (((ltp - prev_close) / prev_close * 100) if prev_close else 0.0))
                if ltp > 0:
                    return ltp, chg_pct, "LIVE"
        except Exception as e:
            log.warning(f"Error fetching live spot from Fyers quotes: {e}")

    return default_price, 0.35, "SIMULATED"


def get_upcoming_expiries(symbol: str = "NIFTY") -> list[str]:
    """
    Returns list of upcoming expiry dates formatted as DD-MMM-YYYY.
    """
    expiries = []
    today = date.today()
    # Find next 4 Thursdays (or Tuesdays for FINNIFTY, Mondays for MIDCPNIFTY, Fridays for SENSEX)
    target_weekday = 3 # Thursday default for NIFTY/BANKNIFTY
    if symbol.upper() == "FINNIFTY":
        target_weekday = 1 # Tuesday
    elif symbol.upper() == "MIDCPNIFTY":
        target_weekday = 0 # Monday
    elif symbol.upper() == "SENSEX":
        target_weekday = 4 # Friday

    curr = today
    while len(expiries) < 4:
        if curr.weekday() == target_weekday and curr >= today:
            expiries.append(curr.strftime("%d-%b-%Y").upper())
        curr += timedelta(days=1)

    return expiries


def fetch_option_chain_data(symbol: str = "NIFTY", expiry: str | None = None, force_refresh: bool = False) -> dict:
    """
    Fetches complete Option Chain data from Fyers API v3 or uses cached/fallback data.
    Ensures 3-minute caching and returns enriched options data.
    """
    symbol = symbol.upper()
    if symbol not in INDEX_CONFIGS:
        symbol = "NIFTY"
    
    cfg = INDEX_CONFIGS[symbol]
    expiries = get_upcoming_expiries(symbol)
    selected_expiry = expiry if expiry and expiry in expiries else expiries[0]

    cache_key = (symbol, selected_expiry)
    now = datetime.now()

    # Check cache
    if not force_refresh and cache_key in _OPTION_CHAIN_CACHE:
        cached = _OPTION_CHAIN_CACHE[cache_key]
        age_seconds = (now - cached["fetch_time"]).total_seconds()
        if age_seconds < _CACHE_TTL_SECONDS:
            return cached["data"]

    # Try fetching from Fyers API
    client = get_fyers_client()
    spot_price, spot_chg, data_status = get_live_spot_price(symbol)
    chain_records = []
    source = "SIMULATED"

    if client:
        try:
            # Fyers V3 Option Chain API
            payload = {
                "symbol": cfg["fyers_symbol"],
                "strikecount": 25,
                "timestamp": ""
            }
            res = client.optionchain(data=payload)
            if res and res.get("s") == "ok" and res.get("data"):
                opt_data = res.get("data", {})
                # Parse strikes and option chain items from Fyers
                options_chain = opt_data.get("optionsChain", [])
                if options_chain:
                    source = "LIVE"
                    data_status = "LIVE"
                    chain_records = _parse_fyers_option_chain(options_chain, cfg, spot_price)
        except Exception as e:
            log.warning(f"Fyers optionchain API call failed: {e}")

    # Fallback to simulated high-fidelity options data if API call returns empty or unavailable
    if not chain_records:
        chain_records = _generate_simulated_option_chain(symbol, cfg, spot_price, selected_expiry)

    # Format result payload
    atm_strike = round(spot_price / cfg["strike_step"]) * cfg["strike_step"]
    
    result = {
        "symbol": symbol,
        "name": cfg["name"],
        "spot_price": round(spot_price, 2),
        "spot_change_pct": round(spot_chg, 2),
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
    }

    # Update cache
    _OPTION_CHAIN_CACHE[cache_key] = {
        "data": result,
        "fetch_time": now,
        "status": data_status
    }

    return result


def _parse_fyers_option_chain(raw_chain: list, cfg: dict, spot: float) -> list[dict]:
    """Parses raw Fyers option chain records into normalized structure."""
    strikes_map = {}
    for item in raw_chain:
        strike = float(item.get("strike_price", 0.0))
        opt_type = item.get("option_type", "").upper()
        ltp = float(item.get("ltp", 0.0) or 0.0)
        oi = int(item.get("oi", 0) or 0)
        prev_oi = int(item.get("prev_oi", oi) or oi)
        change_oi = oi - prev_oi
        volume = int(item.get("volume", 0) or 0)
        iv = float(item.get("iv", 0.0) or 0.0)
        greeks = item.get("greeks", {}) or {}

        if strike not in strikes_map:
            strikes_map[strike] = {
                "strike": strike,
                "ce_ltp": 0.0,
                "ce_oi": 0,
                "ce_change_oi": 0,
                "ce_volume": 0,
                "ce_iv": 0.0,
                "ce_delta": 0.0,
                "pe_ltp": 0.0,
                "pe_oi": 0,
                "pe_change_oi": 0,
                "pe_volume": 0,
                "pe_iv": 0.0,
                "pe_delta": 0.0,
            }

        if opt_type == "CE":
            strikes_map[strike]["ce_ltp"] = ltp
            strikes_map[strike]["ce_oi"] = oi
            strikes_map[strike]["ce_change_oi"] = change_oi
            strikes_map[strike]["ce_volume"] = volume
            strikes_map[strike]["ce_iv"] = iv
            strikes_map[strike]["ce_delta"] = float(greeks.get("delta", 0.0) or 0.0)
        elif opt_type == "PE":
            strikes_map[strike]["pe_ltp"] = ltp
            strikes_map[strike]["pe_oi"] = oi
            strikes_map[strike]["pe_change_oi"] = change_oi
            strikes_map[strike]["pe_volume"] = volume
            strikes_map[strike]["pe_iv"] = iv
            strikes_map[strike]["pe_delta"] = float(greeks.get("delta", 0.0) or 0.0)

    # Convert to sorted list around spot
    sorted_strikes = sorted(strikes_map.keys())
    return [strikes_map[k] for k in sorted_strikes]


def _generate_simulated_option_chain(symbol: str, cfg: dict, spot: float, expiry: str) -> list[dict]:
    """
    Generates realistic, mathematically sound option chain data centered around spot
    for offline testing and off-market hours.
    """
    step = cfg["strike_step"]
    atm = round(spot / step) * step
    strikes = [atm + i * step for i in range(-15, 16)]

    chain = []
    base_iv = 14.5 # approx India VIX level
    r = 0.065 # risk-free rate 6.5%
    days_to_expiry = 4.0 # weekly expiry estimate
    t = days_to_expiry / 365.0

    for strike in strikes:
        moneyness = (strike - spot) / spot
        
        # Approximate Black-Scholes pricing
        d1 = (math.log(spot / strike) + (r + 0.5 * (base_iv/100)**2) * t) / ((base_iv/100) * math.sqrt(t))
        d2 = d1 - (base_iv/100) * math.sqrt(t)

        def norm_cdf(x):
            return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

        ce_price = max(spot * norm_cdf(d1) - strike * math.exp(-r * t) * norm_cdf(d2), 0.05)
        pe_price = max(strike * math.exp(-r * t) * norm_cdf(-d2) - spot * norm_cdf(-d1), 0.05)

        # OI distribution realistic model: high concentration at OTM round strikes
        dist_factor = math.exp(-0.5 * (abs(strike - spot) / (5 * step))**2)
        base_oi = int(1200000 * dist_factor + 150000)

        # Round number bonus (multiples of 500 or 1000 have higher OI)
        if strike % (step * 10) == 0:
            base_oi = int(base_oi * 1.8)
        elif strike % (step * 5) == 0:
            base_oi = int(base_oi * 1.4)

        # CE vs PE bias based on moneyness
        ce_oi = int(base_oi * (1.2 if strike >= atm else 0.75))
        pe_oi = int(base_oi * (1.2 if strike <= atm else 0.75))

        # Change in OI simulation
        ce_change_oi = int(ce_oi * 0.18 * (1.0 if strike >= atm + step else -0.35))
        pe_change_oi = int(pe_oi * 0.22 * (1.2 if strike <= atm else -0.2))

        # Volume
        ce_vol = int(ce_oi * 0.65)
        pe_vol = int(pe_oi * 0.58)

        chain.append({
            "strike": strike,
            "ce_ltp": round(ce_price, 2),
            "ce_oi": ce_oi,
            "ce_change_oi": ce_change_oi,
            "ce_volume": ce_vol,
            "ce_iv": round(base_iv + (abs(strike - spot) / spot) * 10, 2),
            "ce_delta": round(norm_cdf(d1), 2),
            "pe_ltp": round(pe_price, 2),
            "pe_oi": pe_oi,
            "pe_change_oi": pe_change_oi,
            "pe_volume": pe_vol,
            "pe_iv": round(base_iv + (abs(strike - spot) / spot) * 10, 2),
            "pe_delta": round(norm_cdf(d1) - 1.0, 2),
        })

    return chain
