"""
app/services/fyers_auth.py — Secure Fyers Authentication & Token Management
Handles automated TOTP login, clock drift tolerance, daily token caching, and provides authenticated Fyers client instances.
Credentials are read strictly from environment variables and NEVER exposed in UI, responses, or logs.
"""

import os
import json
import logging
import threading
import time
import urllib.parse
from datetime import datetime
import dotenv
import requests
import pyotp

# Load environment variables
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "config", ".env"))
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

log = logging.getLogger("fyers_auth")
_auth_lock = threading.Lock()
_cached_fyers_instance = None
_cached_token_date = None


def get_token_cache_path() -> str:
    """Returns absolute path to token cache file in instance folder."""
    instance_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "instance"))
    os.makedirs(instance_dir, exist_ok=True)
    return os.path.join(instance_dir, "fyers_token.json")


def load_cached_token() -> str | None:
    """Loads today's cached access token if valid."""
    cache_path = get_token_cache_path()
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        today_str = datetime.now().strftime("%Y-%m-%d")
        if data.get("date") == today_str and data.get("access_token"):
            return data["access_token"]
    except Exception as e:
        log.warning(f"Error loading cached token: {e}")
    return None


def save_cached_token(access_token: str) -> None:
    """Saves valid token to instance cache for today."""
    cache_path = get_token_cache_path()
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"access_token": access_token, "date": today_str}, f)
    except Exception as e:
        log.warning(f"Error saving cached token: {e}")


def generate_fyers_access_token() -> tuple[str | None, str | None]:
    """
    Attempts automated TOTP login with Fyers API v3.
    Returns (access_token, error_message).
    """
    raw_client_id = (os.getenv("FYERS_CLIENT_ID") or os.getenv("APP_ID") or "").strip()
    client_id = f"{raw_client_id}-100" if raw_client_id and not raw_client_id.endswith("-100") else raw_client_id
    secret_key = (os.getenv("APP_SECRET") or "").strip()
    redirect_uri = os.getenv("REDIRECT_URI", "http://127.0.0.1:5000/")
    fyers_id = (os.getenv("FYERS_ID") or "").strip()
    pin = str(os.getenv("PIN") or "").strip()
    totp_key = (os.getenv("TOTP_KEY") or "").strip()

    if not all([client_id, secret_key, fyers_id, pin, totp_key]):
        missing = [k for k, v in [("FYERS_CLIENT_ID/APP_ID", client_id), ("APP_SECRET", secret_key),
                                  ("FYERS_ID", fyers_id), ("PIN", pin), ("TOTP_KEY", totp_key)] if not v]
        return None, f"Missing required credentials: {', '.join(missing)}"

    try:
        from fyers_apiv3 import fyersModel
        session = requests.Session()

        # Step 1: Send login OTP
        r1 = session.post(
            "https://api-t2.fyers.in/vagator/v2/send_login_otp",
            json={"fy_id": fyers_id, "app_id": "2"},
            timeout=10
        ).json()
        if r1.get("s") != "ok":
            return None, f"Fyers OTP step: {r1.get('message', 'Failed to request OTP')}"
        req_key1 = r1.get("request_key")

        # Step 2: Verify TOTP with clock drift tolerance (+30s, 0s, -30s)
        totp = pyotp.TOTP(totp_key)
        now = time.time()
        req_key2 = None

        for offset in [30, 0, -30]:
            code = str(totp.at(now + offset))
            r2 = session.post(
                "https://api-t2.fyers.in/vagator/v2/verify_otp",
                json={"request_key": req_key1, "otp": code},
                timeout=10
            ).json()
            if r2.get("s") == "ok":
                req_key2 = r2.get("request_key")
                break

        if not req_key2:
            return None, "Fyers TOTP verification failed across time windows"

        # Step 3: Verify PIN
        r3 = session.post(
            "https://api-t2.fyers.in/vagator/v2/verify_pin",
            json={"request_key": req_key2, "identity_type": "pin", "identifier": pin},
            timeout=10
        ).json()
        if r3.get("s") != "ok":
            return None, f"Fyers PIN verification: {r3.get('message', 'Invalid PIN')}"

        bearer_token = r3.get("data", {}).get("access_token")
        if not bearer_token:
            return None, "Fyers bearer access_token missing in PIN response"

        # Step 4: Token exchange for auth_code
        headers = {"Authorization": f"Bearer {bearer_token}"}
        app_id_prefix = client_id.split("-")[0]

        payload = {
            "fyers_id": fyers_id,
            "app_id": app_id_prefix,
            "redirect_uri": redirect_uri,
            "appType": "100",
            "code_challenge": "",
            "state": "None",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True
        }
        r4 = session.post("https://api-t1.fyers.in/api/v3/token", json=payload, headers=headers, timeout=10).json()
        if r4.get("s") != "ok" and "Url" not in r4 and "auth_code" not in r4:
            return None, f"Fyers Auth Code exchange: {r4.get('message', 'Failed auth exchange')}"

        url_or_code = r4.get("Url") or r4.get("data", {}).get("auth_code") or r4.get("auth_code")
        if url_or_code and "auth_code=" in str(url_or_code):
            parsed = urllib.parse.urlparse(url_or_code)
            auth_code = urllib.parse.parse_qs(parsed.query).get("auth_code", [None])[0]
        else:
            auth_code = url_or_code

        if not auth_code:
            return None, "Fyers auth_code could not be extracted"

        # Step 5: Session Model generate_token
        session_model = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code"
        )
        session_model.set_token(auth_code)
        resp_token = session_model.generate_token()
        if resp_token.get("s") == "ok" and resp_token.get("access_token"):
            access_token = resp_token["access_token"]
            save_cached_token(access_token)
            return access_token, None
        else:
            return None, f"Fyers generate_token failed: {resp_token.get('message', 'Unknown error')}"
    except Exception as e:
        log.error(f"Fyers token generation exception: {e}")
        return None, str(e)


def get_fyers_client():
    """
    Returns an authenticated FyersModel instance.
    Checks cached token first; if unavailable, attempts generation.
    Returns None if authentication cannot be completed.
    """
    global _cached_fyers_instance, _cached_token_date
    with _auth_lock:
        today_str = datetime.now().strftime("%Y-%m-%d")
        if _cached_fyers_instance and _cached_token_date == today_str:
            return _cached_fyers_instance

        # 1. Try cached token
        token = load_cached_token()
        if not token:
            token, err = generate_fyers_access_token()
            if not token:
                log.info(f"Fyers auto-login note: {err}")

        if token:
            try:
                from fyers_apiv3 import fyersModel
                raw_client_id = (os.getenv("FYERS_CLIENT_ID") or os.getenv("APP_ID") or "").strip()
                client_id = f"{raw_client_id}-100" if raw_client_id and not raw_client_id.endswith("-100") else raw_client_id
                client = fyersModel.FyersModel(client_id=client_id, token=token, is_async=False, log_path="")
                _cached_fyers_instance = client
                _cached_token_date = today_str
                return client
            except Exception as e:
                log.error(f"Failed to instantiate FyersModel: {e}")

        return None
