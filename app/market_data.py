"""
app/market_data.py — FYERS Provider Integration & Option Chain Normalization
Handles:
1. Automated Zero-Click TOTP FYERS API v3 Login & Daily Token Caching.
2. Spot price fetching for NIFTY 50 Index.
3. Option chain fetching, strike parsing, and normalization.
4. Quote freshness validation and fallback data simulation for offline testing.
"""

import os
import json
import time
import base64
import logging
import threading
import urllib.parse
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, Tuple, List

import requests
import pyotp
from fyers_apiv3 import fyersModel

from app.config import Config

log = logging.getLogger("market_data")
IST = ZoneInfo("Asia/Kolkata")

_auth_lock = threading.Lock()
TOKEN_CACHE_FILE = os.path.join(Config.INSTANCE_DIR, "fyers_token.json")


class MarketDataProvider:
    """
    Authoritative Market Data Provider integrating FYERS API v3 with automatic TOTP login.
    """
    _client = None
    _access_token = None
    _token_date = None
    _last_auth_attempt = 0.0
    _auth_cooldown_seconds = 300.0  # 5 minutes backoff between failed login attempts

    @classmethod
    def get_access_token(cls) -> Optional[str]:
        """Returns cached access token or performs automated zero-click login with cooldown."""
        with _auth_lock:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")

            # Check direct environment override (e.g. FYERS_ACCESS_TOKEN in config/.env)
            env_token = Config.FYERS_ACCESS_TOKEN or os.environ.get("FYERS_ACCESS_TOKEN") or os.environ.get("ACCESS_TOKEN")
            if env_token and env_token.strip():
                cls._access_token = env_token.strip()
                cls._token_date = today_str
                return cls._access_token

            # Check in-memory cache
            if cls._access_token and cls._token_date == today_str:
                return cls._access_token

            # Check file cache in instance/fyers_token.json
            if os.path.exists(TOKEN_CACHE_FILE):
                try:
                    with open(TOKEN_CACHE_FILE, "r") as f:
                        cached = json.load(f)
                    if cached.get("date") == today_str and cached.get("access_token"):
                        cls._access_token = cached["access_token"]
                        cls._token_date = today_str
                        log.info("Loaded FYERS access token from disk cache.")
                        return cls._access_token
                except Exception as e:
                    log.warning(f"Failed reading token cache: {e}")

            # Respect authentication cooldown if a previous attempt failed
            now_ts = time.time()
            if now_ts - cls._last_auth_attempt < cls._auth_cooldown_seconds:
                return None

            cls._last_auth_attempt = now_ts

            # Perform zero-click TOTP login
            token = cls._authenticate_headless()
            if token:
                cls._access_token = token
                cls._token_date = today_str
                cls._auth_cooldown_seconds = 300.0  # Reset cooldown on success
                try:
                    with open(TOKEN_CACHE_FILE, "w") as f:
                        json.dump({"date": today_str, "access_token": token}, f)
                except Exception as e:
                    log.error(f"Failed saving token cache: {e}")
                return token
            else:
                # Exponential backoff up to 15 minutes to prevent Cloudflare/FYERS IP lockout
                cls._auth_cooldown_seconds = min(cls._auth_cooldown_seconds * 1.5, 900.0)

            return None

    @classmethod
    def _authenticate_headless(cls) -> Optional[str]:
        """Automated zero-click TOTP authentication with FYERS."""
        fy_id = Config.FYERS_ID
        app_id = Config.FYERS_APP_ID
        app_secret = Config.FYERS_APP_SECRET
        totp_key = Config.FYERS_TOTP_KEY
        pin = Config.FYERS_PIN
        redirect_uri = Config.FYERS_REDIRECT_URI

        if not all([fy_id, app_id, app_secret, totp_key, pin]):
            log.warning("FYERS credentials missing in configuration. Running in simulation mode.")
            return None

        try:
            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Content-Type": "application/json"
            })

            # 1. Send Login OTP via TOTP
            totp = pyotp.TOTP(totp_key.replace(" ", "").strip())
            current_otp = totp.now()

            send_otp_url = "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2"
            res1 = session.post(send_otp_url, json={"fy_id": fy_id, "app_id": "2"}, timeout=10)
            if res1.status_code == 429:
                log.warning("FYERS API login endpoint rate-limited (HTTP 429). Cooldown active for 10 minutes; simulation mode engaged.")
                cls._auth_cooldown_seconds = 600.0
                return None
            elif res1.status_code != 200:
                log.warning(f"FYERS Step 1 failed ({res1.status_code}). Running simulation mode.")
                return None

            request_key = res1.json().get("request_key")
            if not request_key:
                log.warning("FYERS Step 1: Missing request_key in response.")
                return None

            # 2. Verify TOTP
            verify_otp_url = "https://api-t2.fyers.in/vagator/v2/verify_otp"
            res2 = session.post(verify_otp_url, json={"request_key": request_key, "otp": current_otp}, timeout=10)
            if res2.status_code == 429:
                log.warning("FYERS Step 2 rate-limited (HTTP 429). Cooldown active.")
                cls._auth_cooldown_seconds = 600.0
                return None
            elif res2.status_code != 200:
                log.warning(f"FYERS Step 2 failed ({res2.status_code}).")
                return None

            request_key_2 = res2.json().get("request_key")
            if not request_key_2:
                log.warning("FYERS Step 2: Missing request_key_2.")
                return None

            # 3. Verify PIN
            verify_pin_url = "https://api-t2.fyers.in/vagator/v2/verify_pin_v2"
            res3 = session.post(verify_pin_url, json={"request_key": request_key_2, "identity_type": "pin", "identifier": pin}, timeout=10)
            if res3.status_code == 429:
                log.warning("FYERS Step 3 rate-limited (HTTP 429). Cooldown active.")
                cls._auth_cooldown_seconds = 600.0
                return None
            elif res3.status_code != 200:
                log.warning(f"FYERS Step 3 failed ({res3.status_code}).")
                return None

            token_data = res3.json().get("data", {})
            auth_token = token_data.get("token")
            if not auth_token:
                log.warning("FYERS Step 3: Missing bearer auth_token.")
                return None

            # 4. Exchange for Auth Code via token API
            app_id_full = f"{app_id}-{Config.FYERS_APP_TYPE}"
            token_url = "https://api-t1.fyers.in/api/v3/token"
            payload_token = {
                "fyers_id": fy_id,
                "app_id": app_id,
                "redirect_uri": redirect_uri,
                "appType": Config.FYERS_APP_TYPE,
                "code_challenge": "",
                "state": "None",
                "scope": "",
                "nonce": "",
                "response_type": "code",
                "create_cookie": True,
            }
            session.headers["Authorization"] = f"Bearer {auth_token}"
            res4 = session.post(token_url, json=payload_token, timeout=10)
            if res4.status_code != 308:
                log.warning(f"FYERS Step 4 failed ({res4.status_code}).")
                return None

            redirect_loc = res4.json().get("Url", "")
            parsed_url = urllib.parse.urlparse(redirect_loc)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            auth_code = query_params.get("auth_code", [None])[0]

            if not auth_code:
                log.warning(f"FYERS Step 4: No auth_code in redirect url: {redirect_loc}")
                return None

            # 5. Generate Access Token using official SDK
            session_model = fyersModel.SessionModel(
                client_id=app_id_full,
                secret_key=app_secret,
                redirect_uri=redirect_uri,
                response_type="code",
                grant_type="authorization_code",
            )
            session_model.set_token(auth_code)
            resp = session_model.generate_token()

            if resp.get("s") == "ok" and resp.get("access_token"):
                log.info("FYERS Zero-Click Authentication Successful.")
                return resp["access_token"]
            else:
                log.error(f"FYERS Step 5 failed: {resp}")
                return None

        except Exception as e:
            log.error(f"FYERS Authentication Exception: {e}")
            return None

    @classmethod
    def get_fyers_client(cls) -> Optional[fyersModel.FyersModel]:
        """Returns authenticated FyersModel client."""
        token = cls.get_access_token()
        if not token:
            return None
        app_id_full = f"{Config.FYERS_APP_ID}-{Config.FYERS_APP_TYPE}"
        return fyersModel.FyersModel(client_id=app_id_full, is_async=False, token=token, log_path="")

    @classmethod
    def get_spot_price(cls) -> Tuple[float, float, str]:
        """
        Returns (spot_price, spot_change_pct, data_provider).
        Falls back to simulation if FYERS is unavailable.
        """
        client = cls.get_fyers_client()
        if client:
            try:
                resp = client.quotes({"symbols": Config.INDEX_FYERS_SYMBOL})
                if resp.get("s") == "ok" and resp.get("d"):
                    quote_data = resp["d"][0].get("v", {})
                    ltp = float(quote_data.get("lp", 0.0) or 0.0)
                    chp = float(quote_data.get("chp", 0.0) or 0.0)
                    if ltp > 0:
                        return ltp, chp, "FYERS_LIVE"
            except Exception as e:
                log.warning(f"FYERS quote error: {e}")

        # Simulated Fallback for offline development / testing
        return 24500.0, 0.35, "SIMULATED_DEMO"

    @classmethod
    def parse_option_quote(cls, opt: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses raw FYERS option chain quote without synthetic price fabrication.
        If bid or ask is missing, non-positive, or empty, it remains 0.0.
        """
        raw_bid = opt.get("bid")
        raw_ask = opt.get("ask")
        raw_ltp = opt.get("ltp")
        raw_oi = opt.get("oi")
        raw_chg_oi = opt.get("oi_change", opt.get("change_oi"))
        raw_vol = opt.get("volume")
        raw_iv = opt.get("iv")

        bid = float(raw_bid) if (raw_bid is not None and str(raw_bid).strip() != "") else 0.0
        ask = float(raw_ask) if (raw_ask is not None and str(raw_ask).strip() != "") else 0.0
        ltp = float(raw_ltp) if (raw_ltp is not None and str(raw_ltp).strip() != "") else 0.0
        oi = int(raw_oi) if (raw_oi is not None and str(raw_oi).strip() != "") else None
        change_oi = int(raw_chg_oi) if (raw_chg_oi is not None and str(raw_chg_oi).strip() != "") else None
        vol = int(raw_vol) if (raw_vol is not None and str(raw_vol).strip() != "") else None
        iv = float(raw_iv) if (raw_iv is not None and str(raw_iv).strip() != "") else 0.0

        return {
            "symbol": opt.get("symbol", ""),
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "oi": oi,
            "change_oi": change_oi,
            "volume": vol,
            "iv": iv,
        }

    @classmethod
    def get_option_chain(cls, expiry: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetches option chain from FYERS or generates validated mock chain.
        Returns normalized dictionary with:
        - spot_price
        - expiry
        - chain_by_strike
        - data_status
        """
        spot, spot_chg, data_status = cls.get_spot_price()
        client = cls.get_fyers_client()

        if client and data_status == "FYERS_LIVE":
            try:
                # 1. Fetch Option Chain from FYERS
                chain_req = {"symbol": Config.INDEX_FYERS_SYMBOL, "strikecount": 12}
                resp = client.optionchain(chain_req)
                if resp.get("s") == "ok" and resp.get("data"):
                    raw_data = resp["data"]
                    exp_dates = raw_data.get("expiryData", [])
                    active_exp = expiry or (exp_dates[0]["date"] if exp_dates else "")

                    options_list = raw_data.get("optionsChain", [])
                    chain_by_strike = {}

                    for opt in options_list:
                        stk = float(opt.get("strike_price", 0.0))
                        opt_type = str(opt.get("option_type", "")).upper()
                        if stk <= 0 or opt_type not in ("CE", "PE"):
                            continue

                        if stk not in chain_by_strike:
                            chain_by_strike[stk] = {"CE": {}, "PE": {}}

                        chain_by_strike[stk][opt_type] = cls.parse_option_quote(opt)

                    if chain_by_strike:
                        return {
                            "spot_price": spot,
                            "spot_change_pct": spot_chg,
                            "expiry": active_exp,
                            "chain_by_strike": chain_by_strike,
                            "data_status": "FYERS_LIVE",
                        }
            except Exception as e:
                log.warning(f"FYERS Option Chain failed: {e}. Falling back to simulated data.")

        # Validated Realistic Simulation Chain
        active_exp = expiry or datetime.now(IST).strftime("%d-%b-%Y").upper()
        chain_by_strike = cls._generate_mock_chain(spot)
        return {
            "spot_price": spot,
            "spot_change_pct": spot_chg,
            "expiry": active_exp,
            "chain_by_strike": chain_by_strike,
            "data_status": "SIMULATED_DEMO",
        }

    @classmethod
    def _generate_mock_chain(cls, spot: float) -> Dict[float, Dict[str, Any]]:
        """Generates realistic mock option quotes around spot for offline testing."""
        atm = round(spot / 50.0) * 50.0
        chain = {}

        # Realistic NIFTY decay table: ATM ~90 pts, 50 OTM ~56 pts, 100 OTM ~34 pts, 150 OTM ~21.5 pts, 200 OTM ~10.5 pts, etc.
        decay_table = {0: 90.0, 1: 56.0, 2: 34.0, 3: 21.5, 4: 10.5, 5: 5.0, 6: 2.5, 7: 1.5, 8: 1.0}

        for i in range(-8, 9):
            stk = atm + (i * 50.0)
            step_otm_ce = max(0, int(round((stk - spot) / 50.0))) if stk >= spot else 0
            step_otm_pe = max(0, int(round((spot - stk) / 50.0))) if stk <= spot else 0

            ce_extrinsic = decay_table.get(step_otm_ce, max(0.5, 90.0 * (0.6 ** max(1, step_otm_ce)))) if stk >= spot else decay_table.get(max(0, int(round((spot - stk) / 50.0))), 2.0)
            pe_extrinsic = decay_table.get(step_otm_pe, max(0.5, 90.0 * (0.6 ** max(1, step_otm_pe)))) if stk <= spot else decay_table.get(max(0, int(round((stk - spot) / 50.0))), 2.0)

            ce_ltp = round(ce_extrinsic if stk >= spot else (spot - stk + ce_extrinsic), 1)
            pe_ltp = round(pe_extrinsic if stk <= spot else (stk - spot + pe_extrinsic), 1)

            ce_bid = round(max(0.5, ce_ltp - 0.5), 1)
            ce_ask = round(ce_ltp + 0.5, 1)
            pe_bid = round(max(0.5, pe_ltp - 0.5), 1)
            pe_ask = round(pe_ltp + 0.5, 1)

            # Heavier Put OI below spot, Call OI above spot
            pe_oi = int(45000 + (30000 if stk == atm - 150 else 0) - (abs(i) * 2000))
            ce_oi = int(42000 + (32000 if stk == atm + 150 else 0) - (abs(i) * 2000))

            chain[stk] = {
                "CE": {
                    "symbol": f"NSE:NIFTY26OCT{int(stk)}CE",
                    "ltp": ce_ltp,
                    "bid": ce_bid,
                    "ask": ce_ask,
                    "oi": max(5000, ce_oi),
                    "change_oi": 3500 if stk >= atm else 1200,
                    "iv": 14.2,
                },
                "PE": {
                    "symbol": f"NSE:NIFTY26OCT{int(stk)}PE",
                    "ltp": pe_ltp,
                    "bid": pe_bid,
                    "ask": pe_ask,
                    "oi": max(5000, pe_oi),
                    "change_oi": 6500 if stk <= atm else 1100,
                    "iv": 15.1,
                },
            }
        return chain
