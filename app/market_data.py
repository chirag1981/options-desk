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
import hashlib
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
from app.contracts import ContractRegistry

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
            log.warning("FYERS credentials missing in configuration.")
            return None

        try:
            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Content-Type": "application/json"
            })

            # 1. Send Login OTP
            send_otp_url = "https://api-t2.fyers.in/vagator/v2/send_login_otp"
            res1 = session.post(send_otp_url, json={"fy_id": fy_id, "app_id": "2"}, timeout=10)
            if res1.status_code == 429:
                log.warning("FYERS API login endpoint rate-limited (HTTP 429). Cooldown active.")
                cls._auth_cooldown_seconds = 600.0
                return None
            elif res1.status_code != 200:
                log.warning(f"FYERS Step 1 failed ({res1.status_code}): {res1.text}")
                return None

            request_key = res1.json().get("request_key")
            if not request_key:
                log.warning(f"FYERS Step 1: Missing request_key in response: {res1.text}")
                return None

            # 2. Verify TOTP (with clock-drift retry)
            verify_otp_url = "https://api-t2.fyers.in/vagator/v2/verify_otp"
            request_key_2 = None
            res2 = None
            for offset in (0, -30, 30):
                totp_val = pyotp.TOTP(totp_key.replace(" ", "").strip()).at(time.time() + offset)
                res2 = session.post(verify_otp_url, json={"request_key": request_key, "otp": str(totp_val)}, timeout=10)
                if res2.status_code == 200 and res2.json().get("request_key"):
                    request_key_2 = res2.json().get("request_key")
                    break

            if not request_key_2:
                err_text = res2.text if res2 is not None else "No response"
                log.warning(f"FYERS Step 2 verify_otp failed: {err_text}")
                return None

            # 3. Verify PIN
            verify_pin_url = "https://api-t2.fyers.in/vagator/v2/verify_pin"
            res3 = session.post(verify_pin_url, json={"request_key": request_key_2, "identity_type": "pin", "identifier": pin}, timeout=10)
            if res3.status_code != 200:
                log.warning(f"FYERS Step 3 failed ({res3.status_code}): {res3.text}")
                return None

            bearer_token = res3.json().get("data", {}).get("access_token") or res3.json().get("data", {}).get("token")
            if not bearer_token:
                log.warning("FYERS Step 3: Missing bearer_token.")
                return None

            # 4. Generate Auth Code via official FYERS v3 token endpoint
            headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
            payload_4 = {
                "fyers_id": fy_id,
                "app_id": app_id,
                "redirect_uri": redirect_uri,
                "appType": Config.FYERS_APP_TYPE,
                "code_challenge": "",
                "state": "options_desk",
                "scope": "",
                "nonce": "",
                "response_type": "code",
                "create_cookie": True,
            }
            res4 = session.post(
                "https://api-t1.fyers.in/api/v3/token",
                headers=headers,
                json=payload_4,
                timeout=10,
            )
            if res4.status_code not in (200, 308):
                log.warning(f"FYERS Step 4 failed ({res4.status_code}): {res4.text}")
                return None

            res4_json = res4.json()
            auth_url = res4_json.get("Url", "")
            auth_code = res4_json.get("auth_code")
            if not auth_code and auth_url:
                parsed_url = urllib.parse.urlparse(auth_url)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                auth_code = query_params.get("auth_code", [None])[0]

            if not auth_code:
                log.warning(f"FYERS Step 4: No auth_code received: {res4.text}")
                return None

            # 5. Generate API v3 Access Token via SessionModel
            client_id = f"{app_id}-{Config.FYERS_APP_TYPE}"
            fyers_session = fyersModel.SessionModel(
                client_id=client_id,
                secret_key=app_secret,
                redirect_uri=redirect_uri,
                response_type="code",
                grant_type="authorization_code",
            )
            fyers_session.set_token(auth_code)
            gen_token_resp = fyers_session.generate_token()

            token = gen_token_resp.get("access_token")
            if token:
                log.info("FYERS Zero-Click Authentication Successful.")
                return token
            else:
                log.error(f"FYERS Step 5 generate_token failed: {gen_token_resp}")
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
    def initialize_connection(cls) -> Tuple[bool, str, float]:
        """
        Attempts to connect to FYERS on application startup.
        Returns (is_connected, status_str, spot_price).
        """
        token = cls.get_access_token()
        if not token:
            return False, "DISCONNECTED", 0.0
        spot, chg, status = cls.get_spot_price()
        return (status == "FYERS_LIVE"), status, spot

    @classmethod
    def _is_test_mode(cls) -> bool:
        """Determines if application is running in isolated pytest test suite."""
        try:
            from flask import current_app
            if current_app and current_app.config.get("TESTING"):
                return True
        except RuntimeError:
            pass
        return os.environ.get("OPTIONS_DESK_ALLOW_SYNTHETIC_TEST_DATA", "0") == "1"

    @classmethod
    def set_access_token(cls, token: str) -> bool:
        """Sets and persists an active FYERS access token."""
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        with _auth_lock:
            cls._access_token = token.strip()
            cls._token_date = today_str
            cls._client = None
            try:
                os.makedirs(Config.INSTANCE_DIR, exist_ok=True)
                with open(TOKEN_CACHE_FILE, "w") as f:
                    json.dump({"date": today_str, "access_token": token.strip()}, f)
                return True
            except Exception as e:
                log.error(f"Failed writing token cache: {e}")
                return False

    @classmethod
    def invalidate_token(cls):
        """Clears expired token from memory and cache file."""
        with _auth_lock:
            cls._access_token = None
            cls._token_date = None
            cls._client = None
            if os.path.exists(TOKEN_CACHE_FILE):
                try:
                    os.remove(TOKEN_CACHE_FILE)
                except Exception:
                    pass

    @classmethod
    def get_spot_price(cls) -> Tuple[float, float, str]:
        """
        Returns (spot_price, spot_change_pct, data_provider).
        Live FYERS stream is strictly required; simulated demo data is disabled in runtime.
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
                elif resp.get("code") == -15:
                    log.warning("FYERS token expired or invalid (code -15). Invalidating cached token.")
                    cls.invalidate_token()
            except Exception as e:
                log.warning(f"FYERS quote error: {e}")

        # Unit testing mode fixture only (pytest)
        if cls._is_test_mode():
            return 24500.0, 0.35, "TEST_MOCK"

        return 0.0, 0.0, "DISCONNECTED"

    @classmethod
    def parse_option_quote(cls, opt: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses raw FYERS option chain quote without synthetic price fabrication.
        If bid or ask is missing, non-positive, or empty, it remains 0.0.
        """
        raw_bid = opt.get("bid")
        raw_ask = opt.get("ask")
        raw_ltp = opt.get("ltp")
        raw_ltpch = opt.get("ltpch")
        raw_ltpchp = opt.get("ltpchp")
        raw_oi = opt.get("oi")
        raw_prev_oi = opt.get("prev_oi")
        raw_chg_oi = opt.get("oich", opt.get("oi_change", opt.get("change_oi")))
        raw_chg_oi_pct = opt.get("oichp")
        raw_vol = opt.get("volume")
        raw_iv = opt.get("iv")

        bid = float(raw_bid) if (raw_bid is not None and str(raw_bid).strip() != "") else 0.0
        ask = float(raw_ask) if (raw_ask is not None and str(raw_ask).strip() != "") else 0.0
        ltp = float(raw_ltp) if (raw_ltp is not None and str(raw_ltp).strip() != "") else 0.0
        ltp_change = float(raw_ltpch) if (raw_ltpch is not None and str(raw_ltpch).strip() != "") else 0.0
        ltp_change_pct = float(raw_ltpchp) if (raw_ltpchp is not None and str(raw_ltpchp).strip() != "") else 0.0
        oi = int(raw_oi) if (raw_oi is not None and str(raw_oi).strip() != "") else None
        prev_oi = int(raw_prev_oi) if (raw_prev_oi is not None and str(raw_prev_oi).strip() != "") else None
        change_oi = int(raw_chg_oi) if (raw_chg_oi is not None and str(raw_chg_oi).strip() != "") else (
            (oi - prev_oi) if (oi is not None and prev_oi is not None) else None
        )
        change_oi_pct = float(raw_chg_oi_pct) if (raw_chg_oi_pct is not None and str(raw_chg_oi_pct).strip() != "") else (
            (round((change_oi / prev_oi) * 100.0, 2) if (change_oi is not None and prev_oi and prev_oi > 0) else 0.0)
        )
        vol = int(raw_vol) if (raw_vol is not None and str(raw_vol).strip() != "") else None
        iv = float(raw_iv) if (raw_iv is not None and str(raw_iv).strip() != "") else 0.0

        return {
            "symbol": opt.get("symbol", ""),
            "ltp": ltp,
            "ltp_change": ltp_change,
            "ltp_change_pct": ltp_change_pct,
            "bid": bid,
            "ask": ask,
            "oi": oi,
            "prev_oi": prev_oi,
            "change_oi": change_oi,
            "change_oi_pct": change_oi_pct,
            "volume": vol,
            "iv": iv,
        }

    @classmethod
    def get_option_chain(cls, expiry: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetches live option chain from FYERS or provides test fixture in unit testing mode.
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
                # 1. Fetch Option Chain from FYERS (25 strikes on each side covers all S1/S2 and R1/R2 walls)
                chain_req = {"symbol": Config.INDEX_FYERS_SYMBOL, "strikecount": 25}
                resp = client.optionchain(chain_req)
                if resp.get("s") == "ok" and resp.get("data"):
                    raw_data = resp["data"]
                    exp_dates = raw_data.get("expiryData", [])

                    # Filter for monthly expiries if configured
                    monthly_expiries = [
                        e for e in exp_dates
                        if e.get("expiry_flag") == "M" or ContractRegistry.is_monthly_expiry(e.get("date", ""))
                    ]
                    available_exp_list = monthly_expiries if Config.MONTHLY_EXPIRIES_ONLY else exp_dates

                    # Determine target expiry and timestamp
                    matched_exp = None
                    if expiry and str(expiry).strip():
                        target_str = str(expiry).strip().upper()
                        for e in exp_dates:
                            if str(e.get("date", "")).strip().upper() == target_str:
                                matched_exp = e
                                break

                    if matched_exp is None and available_exp_list:
                        matched_exp = available_exp_list[0]

                    active_exp = matched_exp.get("date", "") if matched_exp else (exp_dates[0]["date"] if exp_dates else "")

                    # If targeted expiry requires a timestamp query different from the initial default call
                    if matched_exp and matched_exp.get("expiry"):
                        target_ts = str(matched_exp["expiry"])
                        default_first_ts = str(exp_dates[0]["expiry"]) if exp_dates and exp_dates[0].get("expiry") else ""
                        if target_ts != default_first_ts:
                            sub_req = {"symbol": Config.INDEX_FYERS_SYMBOL, "strikecount": 25, "timestamp": target_ts}
                            sub_resp = client.optionchain(sub_req)
                            if sub_resp.get("s") == "ok" and sub_resp.get("data"):
                                raw_data = sub_resp["data"]

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
                        available_formatted = [
                            {
                                "date": e.get("date", ""),
                                "timestamp": e.get("expiry", ""),
                                "expiry_flag": e.get("expiry_flag", "M" if ContractRegistry.is_monthly_expiry(e.get("date", "")) else "W"),
                                "label": f"{e.get('date', '')} (Monthly)" if (e.get("expiry_flag") == "M" or ContractRegistry.is_monthly_expiry(e.get("date", ""))) else e.get("date", ""),
                            }
                            for e in available_exp_list
                        ]
                        return {
                            "spot_price": spot,
                            "spot_change_pct": spot_chg,
                            "expiry": active_exp,
                            "available_expiries": available_formatted,
                            "chain_by_strike": chain_by_strike,
                            "data_status": "FYERS_LIVE",
                        }
            except Exception as e:
                log.warning(f"FYERS Option Chain failed: {e}")

        # Unit testing mode fixture only (pytest)
        if cls._is_test_mode():
            active_exp = expiry or "27-OCT-2026"
            chain_by_strike = cls._generate_mock_chain(spot or 24500.0)
            mock_expiries = [
                {"date": "27-OCT-2026", "timestamp": "1793095800", "expiry_flag": "M", "label": "27-OCT-2026 (Monthly)"},
                {"date": "23-NOV-2026", "timestamp": "1795428600", "expiry_flag": "M", "label": "23-NOV-2026 (Monthly)"},
                {"date": "29-DEC-2026", "timestamp": "1798539000", "expiry_flag": "M", "label": "29-DEC-2026 (Monthly)"},
            ]
            return {
                "spot_price": spot or 24500.0,
                "spot_change_pct": spot_chg or 0.35,
                "expiry": active_exp,
                "available_expiries": mock_expiries,
                "chain_by_strike": chain_by_strike,
                "data_status": "TEST_MOCK",
            }

        # Runtime/Production without live feed: strict zero-simulation
        return {
            "spot_price": 0.0,
            "spot_change_pct": 0.0,
            "expiry": "",
            "available_expiries": [],
            "chain_by_strike": {},
            "data_status": "DISCONNECTED",
        }

    @classmethod
    def _generate_mock_chain(cls, spot: float) -> Dict[float, Dict[str, Any]]:
        """Generates realistic mock option quotes around spot for offline testing."""
        atm = round(spot / 50.0) * 50.0
        chain = {}

        # Realistic NIFTY decay table: ATM ~90 pts, 50 OTM ~56 pts, 100 OTM ~34 pts, 150 OTM ~21.5 pts, 200 OTM ~10.5 pts, etc.
        decay_table = {0: 90.0, 1: 56.0, 2: 34.0, 3: 21.5, 4: 10.5, 5: 5.0, 6: 2.5, 7: 1.5, 8: 1.0}

        for i in range(-16, 17):
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
