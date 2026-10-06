"""
app/services/fyers_auth.py — Automated Headless FYERS Authentication & Token Manager
Handles automated zero-click TOTP login, daily token caching,
and provides authenticated FyersModel clients for REST APIs & Option Chains.
"""

import os
import json
import time
import logging
import threading
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import pyotp
import dotenv
from fyers_apiv3 import fyersModel

# Load environment variables
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "config", ".env"))
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

log = logging.getLogger("fyers_auth")
IST = ZoneInfo("Asia/Kolkata")
_auth_lock = threading.Lock()

TOKEN_CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "instance"))
TOKEN_CACHE_FILE = os.path.join(TOKEN_CACHE_DIR, "fyers_token.json")

FYERS_INDEX_SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}


def get_cached_token() -> str | None:
    """Loads today's cached session token if valid."""
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        if data.get("date") == today_str and data.get("token"):
            return data["token"]
    except Exception as e:
        log.warning(f"Error reading Fyers token cache: {e}")
    return None


def save_token(token: str, fyers_id: str) -> None:
    """Saves session token to instance/fyers_token.json with timestamp."""
    os.makedirs(TOKEN_CACHE_DIR, exist_ok=True)
    now_ist = datetime.now(IST)
    data = {
        "fyers_id": fyers_id,
        "token": token,
        "date": now_ist.strftime("%Y-%m-%d"),
        "created_at": now_ist.isoformat()
    }
    with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("FYERS daily token saved to instance/fyers_token.json")


def generate_fyers_access_token() -> tuple[str | None, str | None]:
    """
    Performs 100% Automated Headless TOTP login with FYERS API.
    Returns (access_token, error_message).
    """
    fy_id = os.getenv("FYERS_ID", "").strip(" \"'")
    pin = os.getenv("PIN", "").strip(" \"'")
    app_id = os.getenv("APP_ID", "").strip(" \"'")
    app_type = os.getenv("APP_TYPE", "100").strip(" \"'")
    app_secret = os.getenv("APP_SECRET", "").strip(" \"'")
    totp_key = os.getenv("TOTP_KEY", "").strip(" \"'")
    redirect_uri = os.getenv("REDIRECT_URI", "http://127.0.0.1:5000/").strip(" \"'")

    if not all([fy_id, pin, app_id, app_secret, totp_key]):
        missing = [k for k, v in [
            ("FYERS_ID", fy_id),
            ("PIN", pin),
            ("APP_ID", app_id),
            ("APP_SECRET", app_secret),
            ("TOTP_KEY", totp_key)
        ] if not v]
        return None, f"Missing required FYERS credentials in config/.env: {', '.join(missing)}"

    client_id = f"{app_id}-{app_type}"

    with _auth_lock:
        # Check cache first
        cached = get_cached_token()
        if cached:
            return cached, None

        log.info("Initiating automated zero-click FYERS headless login...")

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })

        try:
            # Step 1: Send Login OTP
            r1 = session.post("https://api-t2.fyers.in/vagator/v2/send_login_otp", json={"fy_id": fy_id, "app_id": "2"}, timeout=10)
            if r1.status_code != 200:
                return None, f"Step 1 send_login_otp failed ({r1.status_code}): {r1.text}"
            req_key = r1.json().get("request_key")
            if not req_key:
                return None, f"Step 1 did not return request_key: {r1.text}"

            # Step 2: Verify TOTP (with clock-drift retry)
            req_key2 = None
            for offset in (0, -30, 30):
                totp_val = pyotp.TOTP(totp_key).at(time.time() + offset)
                r2 = session.post("https://api-t2.fyers.in/vagator/v2/verify_otp", json={"request_key": req_key, "otp": str(totp_val)}, timeout=10)
                if r2.status_code == 200 and r2.json().get("request_key"):
                    req_key2 = r2.json().get("request_key")
                    break

            if not req_key2:
                return None, f"Step 2 verify_otp failed: {r2.text}"

            # Step 3: Verify PIN
            r3 = session.post("https://api-t2.fyers.in/vagator/v2/verify_pin", json={"request_key": req_key2, "identity_type": "pin", "identifier": pin}, timeout=10)
            if r3.status_code != 200:
                return None, f"Step 3 verify_pin failed ({r3.status_code}): {r3.text}"
            r3_data = r3.json().get("data", {})
            bearer_token = r3_data.get("access_token") or r3_data.get("token_result", {}).get("access_token")
            if not bearer_token:
                return None, f"Step 3 missing bearer access_token: {r3.text}"

            # Step 4: Request Auth Code URL
            headers = {"Authorization": f"Bearer {bearer_token}"}
            payload = {
                "fyers_id": fy_id,
                "app_id": app_id,
                "redirect_uri": redirect_uri,
                "appType": app_type,
                "code_challenge": "",
                "state": "options_desk_session",
                "scope": "",
                "nonce": "",
                "response_type": "code",
                "create_cookie": True
            }
            r4 = session.post("https://api-t1.fyers.in/api/v3/token", headers=headers, json=payload, timeout=10)
            auth_url = r4.json().get("Url")
            if not auth_url:
                return None, f"Step 4 failed to obtain auth_url: {r4.text}"

            parsed = urllib.parse.urlparse(auth_url)
            qs = urllib.parse.parse_qs(parsed.query)
            auth_code = qs.get("auth_code", [""])[0]
            if not auth_code:
                return None, f"Step 4 URL missing auth_code parameter: {auth_url}"

            # Step 5: Generate API v3 Access Token
            fyers_session = fyersModel.SessionModel(
                client_id=client_id,
                secret_key=app_secret,
                redirect_uri=redirect_uri,
                response_type="code",
                grant_type="authorization_code"
            )
            fyers_session.set_token(auth_code)
            gen_token_resp = fyers_session.generate_token()

            access_token = gen_token_resp.get("access_token")
            if not access_token:
                return None, f"Step 5 token generation failed: {gen_token_resp.get('message') or gen_token_resp}"

            save_token(access_token, fy_id)
            log.info(f"Automated FYERS login SUCCESSFUL for user {fy_id}")
            return access_token, None

        except Exception as e:
            log.error(f"FYERS automated login exception: {e}")
            return None, str(e)


def get_fyers_token() -> str | None:
    """
    Returns a valid FYERS session token.
    Loads from cache if valid today; otherwise automatically performs headless login.
    """
    cached = get_cached_token()
    if cached:
        return cached

    token, err = generate_fyers_access_token()
    if token:
        return token
    else:
        log.error(f"Failed to auto-authenticate FYERS: {err}")
        return None


def get_fyers_model() -> fyersModel.FyersModel | None:
    """Returns an authenticated FyersModel client instance."""
    token = get_fyers_token()
    if not token:
        return None
    app_id = os.getenv("APP_ID", "").strip(" \"'")
    app_type = os.getenv("APP_TYPE", "100").strip(" \"'")
    client_id = f"{app_id}-{app_type}"
    return fyersModel.FyersModel(client_id=client_id, is_async=False, token=token, log_path="")


def get_fyers_index_spot(symbol: str = "NIFTY") -> tuple[float, float, str] | None:
    """
    Fetches real-time spot price and day change % from FYERS quotes API.
    Returns (spot_price, change_pct, 'LIVE') or None.
    """
    sym = symbol.upper()
    fy_sym = FYERS_INDEX_SYMBOLS.get(sym)
    if not fy_sym:
        return None

    fyers = get_fyers_model()
    if not fyers:
        return None

    try:
        data = {"symbols": fy_sym}
        resp = fyers.quotes(data=data)
        if resp.get("s") == "ok":
            d_list = resp.get("d", [])
            if d_list:
                item = d_list[0].get("v", {})
                ltp = float(item.get("lp", 0.0) or 0.0)
                chg_pct = float(item.get("chp", 0.0) or 0.0)
                if ltp > 0:
                    return ltp, chg_pct, "LIVE"
    except Exception as e:
        log.warning(f"FYERS spot quote error for {symbol}: {e}")
    return None


def get_fyers_option_ltp(symbol: str, strike: float, signal_type: str, expiry: str | None = None) -> float | None:
    """
    Fetches live LTP for a specific options contract from FYERS option chain / quotes API.
    """
    sym = symbol.upper()
    fy_sym = FYERS_INDEX_SYMBOLS.get(sym, "NSE:NIFTY50-INDEX")
    fyers = get_fyers_model()
    if not fyers:
        return None

    try:
        oc = fyers.optionchain(data={"symbol": fy_sym, "strikecount": 12})
        if oc.get("s") == "ok":
            chain = oc.get("data", {}).get("optionsChain", [])
            strike_val = float(strike)
            sig_type = signal_type.upper()
            for c in chain:
                if abs(float(c.get("strike_price", -1)) - strike_val) < 0.1 and c.get("option_type") == sig_type:
                    ltp = float(c.get("ltp", 0.0) or 0.0)
                    if ltp > 0:
                        return ltp
    except Exception as e:
        log.warning(f"FYERS option LTP fetch error for {symbol} {strike} {signal_type}: {e}")
    return None
