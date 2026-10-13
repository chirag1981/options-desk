"""
app/services/fyers_auth.py — Automated Headless FYERS Authentication & Token Manager
Handles automated zero-click TOTP login, daily token caching,
and provides authenticated FyersModel clients for REST APIs & Option Chains.
"""

import os
import json
import time
import base64
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

_last_auth_attempt_time: float = 0.0
_last_auth_error: str | None = None
_last_logged_cooldown_time: float = 0.0
_last_logged_auth_err_time: float = 0.0
_last_logged_auth_err_msg: str | None = None

_AUTH_COOLDOWN_SECONDS: int = 60
_AUTH_429_COOLDOWN_SECONDS: int = 180  # 3 minutes for 429 rate limit recovery

TOKEN_CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "instance"))
TOKEN_CACHE_FILE = os.path.join(TOKEN_CACHE_DIR, "fyers_token.json")
AUTH_COOLDOWN_FILE = os.path.join(TOKEN_CACHE_DIR, "fyers_auth_cooldown.json")
AUTH_LOCK_PATH = os.path.join(TOKEN_CACHE_DIR, "fyers_auth.exclusive.lock")

FYERS_INDEX_SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
}


def is_jwt_valid(token: str, buffer_seconds: int = 300) -> bool:
    """Checks whether token is a valid JWT whose expiration timestamp is in the future."""
    if not token or not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        payload_b64 = parts[1]
        rem = len(payload_b64) % 4
        if rem > 0:
            payload_b64 += "=" * (4 - rem)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload_data = json.loads(payload_bytes.decode("utf-8"))
        exp = payload_data.get("exp")
        if exp is not None:
            return time.time() < (float(exp) - buffer_seconds)
    except Exception as e:
        log.debug(f"JWT expiry validation check failed: {e}")
    return False


def _get_persisted_cooldown() -> tuple[float, str | None]:
    """Reads persisted cooldown expiry and last error across processes."""
    if not os.path.exists(AUTH_COOLDOWN_FILE):
        return 0.0, None
    try:
        with open(AUTH_COOLDOWN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("cooldown_until", 0.0)), data.get("last_error")
    except Exception:
        return 0.0, None


def _set_persisted_cooldown(cooldown_seconds: int, error_msg: str) -> None:
    """Persists cooldown to file so all processes/restarts respect the cooldown."""
    os.makedirs(TOKEN_CACHE_DIR, exist_ok=True)
    try:
        data = {
            "cooldown_until": time.time() + cooldown_seconds,
            "last_error": error_msg,
            "created_at": datetime.now(IST).isoformat(),
        }
        with open(AUTH_COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.debug(f"Error saving auth cooldown: {e}")


def _clear_persisted_cooldown() -> None:
    """Removes persisted cooldown upon successful login."""
    try:
        if os.path.exists(AUTH_COOLDOWN_FILE):
            os.remove(AUTH_COOLDOWN_FILE)
    except Exception:
        pass


class _CrossProcessAuthLock:
    """Context manager acquiring an exclusive file lock to prevent multi-process login stampedes."""
    def __init__(self, lock_path: str, timeout_sec: float = 12.0):
        self.lock_path = lock_path
        self.timeout_sec = timeout_sec
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        start = time.time()
        while time.time() - start < self.timeout_sec:
            try:
                self.fd = open(self.lock_path, "a+")
                if os.name == "nt":
                    import msvcrt
                    self.fd.seek(0)
                    msvcrt.locking(self.fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (IOError, OSError):
                if self.fd:
                    try:
                        self.fd.close()
                    except Exception:
                        pass
                    self.fd = None
                time.sleep(0.4)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fd.seek(0)
                    msvcrt.locking(self.fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
                self.fd.close()
            except Exception:
                pass
            self.fd = None


def get_cached_token() -> str | None:
    """
    Loads cached session token if valid.
    Validates cryptographic JWT expiration timestamp so tokens remain active for
    their entire authorized lifespan regardless of overnight boundary crossings.
    """
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("token")
        if token and is_jwt_valid(token, buffer_seconds=300):
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            if data.get("date") != today_str:
                data["date"] = today_str
                try:
                    with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as wf:
                        json.dump(data, wf, indent=2)
                except Exception:
                    pass
            return token
    except Exception as e:
        log.warning(f"Error reading Fyers token cache: {e}")
    return None


def invalidate_cached_token() -> None:
    """Removes cached token file when token is explicitly revoked or expired."""
    try:
        if os.path.exists(TOKEN_CACHE_FILE):
            os.remove(TOKEN_CACHE_FILE)
            log.info("Invalidated FYERS cached token.")
    except Exception as e:
        log.warning(f"Error removing cached token: {e}")


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
    Protects against 429 Too Many Requests cascades via cross-process locking,
    persistent cooldown, and JWT expiry validation.
    """
    global _last_auth_attempt_time, _last_auth_error, _last_logged_cooldown_time

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
        with _CrossProcessAuthLock(AUTH_LOCK_PATH):
            # Check cache first (another process or thread may have just generated it)
            cached = get_cached_token()
            if cached:
                return cached, None

            # Check persistent cooldown to prevent 429 Too Many Requests spam
            now = time.time()
            persisted_until, persisted_err = _get_persisted_cooldown()
            active_err = persisted_err or _last_auth_error

            cooldown = _AUTH_429_COOLDOWN_SECONDS if (active_err and "429" in str(active_err)) else _AUTH_COOLDOWN_SECONDS
            max_until = max(persisted_until, _last_auth_attempt_time + cooldown)

            if now < max_until:
                remaining = int(max_until - now)
                msg = f"FYERS authentication in cooldown ({remaining}s remaining to prevent rate-limiting). Previous error: {active_err}"
                if (now - _last_logged_cooldown_time) >= 30.0:
                    log.warning(msg)
                    _last_logged_cooldown_time = now
                return None, msg

            _last_auth_attempt_time = now
            log.info("Initiating automated zero-click FYERS headless login...")

            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            })

            try:
                # Step 1: Send Login OTP
                r1 = session.post("https://api-t2.fyers.in/vagator/v2/send_login_otp", json={"fy_id": fy_id, "app_id": "2"}, timeout=10)
                if r1.status_code != 200:
                    err_text = r1.text
                    cd = _AUTH_429_COOLDOWN_SECONDS if r1.status_code == 429 else _AUTH_COOLDOWN_SECONDS
                    _last_auth_error = f"Step 1 send_login_otp failed ({r1.status_code}): {err_text}"
                    _set_persisted_cooldown(cd, _last_auth_error)
                    return None, _last_auth_error

                req_key = r1.json().get("request_key")
                if not req_key:
                    _last_auth_error = f"Step 1 did not return request_key: {r1.text}"
                    _set_persisted_cooldown(_AUTH_COOLDOWN_SECONDS, _last_auth_error)
                    return None, _last_auth_error

                # Step 2: Verify TOTP (with clock-drift retry)
                req_key2 = None
                for offset in (0, -30, 30):
                    totp_val = pyotp.TOTP(totp_key).at(time.time() + offset)
                    r2 = session.post("https://api-t2.fyers.in/vagator/v2/verify_otp", json={"request_key": req_key, "otp": str(totp_val)}, timeout=10)
                    if r2.status_code == 200 and r2.json().get("request_key"):
                        req_key2 = r2.json().get("request_key")
                        break

                if not req_key2:
                    _last_auth_error = f"Step 2 verify_otp failed: {r2.text}"
                    _set_persisted_cooldown(_AUTH_COOLDOWN_SECONDS, _last_auth_error)
                    return None, _last_auth_error

                # Step 3: Verify PIN
                r3 = session.post("https://api-t2.fyers.in/vagator/v2/verify_pin", json={"request_key": req_key2, "identity_type": "pin", "identifier": pin}, timeout=10)
                if r3.status_code != 200:
                    _last_auth_error = f"Step 3 verify_pin failed ({r3.status_code}): {r3.text}"
                    _set_persisted_cooldown(_AUTH_COOLDOWN_SECONDS, _last_auth_error)
                    return None, _last_auth_error
                r3_data = r3.json().get("data", {})
                bearer_token = r3_data.get("access_token") or r3_data.get("token_result", {}).get("access_token")
                if not bearer_token:
                    _last_auth_error = f"Step 3 missing bearer access_token: {r3.text}"
                    _set_persisted_cooldown(_AUTH_COOLDOWN_SECONDS, _last_auth_error)
                    return None, _last_auth_error

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
                    _last_auth_error = f"Step 4 failed to obtain auth_url: {r4.text}"
                    _set_persisted_cooldown(_AUTH_COOLDOWN_SECONDS, _last_auth_error)
                    return None, _last_auth_error

                parsed = urllib.parse.urlparse(auth_url)
                qs = urllib.parse.parse_qs(parsed.query)
                auth_code = qs.get("auth_code", [""])[0]
                if not auth_code:
                    _last_auth_error = f"Step 4 URL missing auth_code parameter: {auth_url}"
                    _set_persisted_cooldown(_AUTH_COOLDOWN_SECONDS, _last_auth_error)
                    return None, _last_auth_error

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
                    _last_auth_error = f"Step 5 token generation failed: {gen_token_resp.get('message') or gen_token_resp}"
                    _set_persisted_cooldown(_AUTH_COOLDOWN_SECONDS, _last_auth_error)
                    return None, _last_auth_error

                _last_auth_error = None
                _clear_persisted_cooldown()
                save_token(access_token, fy_id)
                log.info(f"Automated FYERS login SUCCESSFUL for user {fy_id}")
                return access_token, None

            except Exception as e:
                _last_auth_error = str(e)
                _set_persisted_cooldown(_AUTH_COOLDOWN_SECONDS, _last_auth_error)
                log.error(f"FYERS automated login exception: {e}")
                return None, str(e)


def get_fyers_token() -> str | None:
    """
    Returns a valid FYERS session token.
    Loads from cache if valid; otherwise automatically performs headless login.
    Throttles error logs to prevent spamming logs during UI polling loops.
    """
    global _last_logged_auth_err_time, _last_logged_auth_err_msg

    cached = get_cached_token()
    if cached:
        return cached

    token, err = generate_fyers_access_token()
    if token:
        _last_logged_auth_err_msg = None
        return token
    else:
        now = time.time()
        if err != _last_logged_auth_err_msg or (now - _last_logged_auth_err_time) >= 30.0:
            log.error(f"Failed to auto-authenticate FYERS: {err}")
            _last_logged_auth_err_time = now
            _last_logged_auth_err_msg = err
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
        elif resp.get("code") in (-16, 401) or "invalid or expired" in str(resp.get("message", "")).lower():
            log.warning(f"FYERS session token expired or invalid (code {resp.get('code')}); invalidating cached token.")
            invalidate_cached_token()
    except Exception as e:
        log.warning(f"FYERS spot quote error for {symbol}: {e}")
    return None


def get_fyers_index_ohlc_details(symbol: str = "NIFTY") -> dict | None:
    """
    Fetches real-time spot price, day change %, open, high, low, and prev_close from FYERS quotes API.
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
                open_p = float(item.get("open_price", 0.0) or 0.0)
                high_p = float(item.get("high_price", 0.0) or 0.0)
                low_p = float(item.get("low_price", 0.0) or 0.0)
                prev_close = float(item.get("prev_close_price", 0.0) or 0.0)
                if ltp > 0:
                    return {
                        "spot_price": ltp,
                        "spot_change_pct": chg_pct,
                        "open_price": open_p if open_p > 0 else ltp,
                        "high_price": high_p if high_p > 0 else ltp,
                        "low_price": low_p if low_p > 0 else ltp,
                        "prev_close": prev_close if prev_close > 0 else (round(ltp / (1.0 + chg_pct / 100.0), 2) if chg_pct != -100 else ltp),
                        "status": "LIVE"
                    }
        elif resp.get("code") in (-16, 401) or "invalid or expired" in str(resp.get("message", "")).lower():
            log.warning(f"FYERS session token expired or invalid (code {resp.get('code')}); invalidating cached token.")
            invalidate_cached_token()
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
        elif oc.get("code") in (-16, 401) or "invalid or expired" in str(oc.get("message", "")).lower():
            log.warning(f"FYERS session token expired or invalid (code {oc.get('code')}); invalidating cached token.")
            invalidate_cached_token()
    except Exception as e:
        log.warning(f"FYERS option LTP fetch error for {symbol} {strike} {signal_type}: {e}")
    return None
