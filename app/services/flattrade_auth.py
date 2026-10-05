"""
app/services/flattrade_auth.py — Automated Headless FlatTrade Authentication & Token Manager
Handles automated zero-click TOTP login, clock-drift tolerance, daily token caching,
and automatic background token refreshment for FlatTrade REST API & PiConnect.
"""

import os
import json
import time
import hashlib
import logging
import threading
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import requests
import pyotp
import dotenv

# Load environment variables
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "config", ".env"))
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

log = logging.getLogger("flattrade_auth")
_auth_lock = threading.Lock()

TOKEN_CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "instance"))
TOKEN_CACHE_FILE = os.path.join(TOKEN_CACHE_DIR, "flattrade_token.json")


def get_cached_token() -> str | None:
    """Loads today's cached session token if valid."""
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        today_str = datetime.now().strftime("%Y-%m-%d")
        if data.get("date") == today_str and data.get("token"):
            return data["token"]
    except Exception as e:
        log.warning(f"Error reading FlatTrade token cache: {e}")
    return None


def save_token(token: str, user_id: str) -> None:
    """Saves session token to instance/flattrade_token.json with timestamp."""
    os.makedirs(TOKEN_CACHE_DIR, exist_ok=True)
    data = {
        "user_id": user_id,
        "token": token,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "created_at": datetime.now().isoformat()
    }
    with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("FlatTrade daily token saved to instance/flattrade_token.json")


def generate_flattrade_access_token() -> tuple[str | None, str | None]:
    """
    Performs 100% Automated Headless TOTP login with FlatTrade API.
    Returns (access_token, error_message).
    """
    user_id = (os.getenv("FLATTRADE_USER_ID") or "").strip()
    password = (os.getenv("FLATTRADE_PASSWORD") or "").strip()
    api_key = (os.getenv("FLATTRADE_API_KEY") or "").strip()
    api_secret = (os.getenv("FLATTRADE_API_SECRET") or "").strip()
    totp_key = (os.getenv("FLATTRADE_TOTP_KEY") or "").strip()

    if not all([user_id, password, api_key, api_secret, totp_key]):
        missing = [k for k, v in [
            ("FLATTRADE_USER_ID", user_id),
            ("FLATTRADE_PASSWORD", password),
            ("FLATTRADE_API_KEY", api_key),
            ("FLATTRADE_API_SECRET", api_secret),
            ("FLATTRADE_TOTP_KEY", totp_key)
        ] if not v]
        return None, f"Missing required FlatTrade credentials in config/.env: {', '.join(missing)}"

    with _auth_lock:
        # Check cache first
        cached = get_cached_token()
        if cached:
            return cached, None

        log.info("Initiating automated zero-click FlatTrade login...")

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://auth.flattrade.in",
            "Referer": f"https://auth.flattrade.in/?app_key={api_key}",
        })

        try:
            # Step 1: Request Session ID (Sid)
            r1 = session.post("https://authapi.flattrade.in/auth/session", headers={"Content-Type": "application/json"}, timeout=10)
            sid = r1.text.strip().replace('"', '')

            if not sid or len(sid) < 16:
                return None, f"Failed to obtain auth session from FlatTrade (Status: {r1.status_code})"

            # Step 2: Compute Password Hash
            pwd_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()

            # Step 3: TOTP with clock-drift tolerance (+30s, 0s, -30s)
            totp = pyotp.TOTP(totp_key)
            now = time.time()
            auth_success = False
            redirect_url = None
            last_emsg = "TOTP validation failed"

            for offset in [0, 30, -30]:
                code = str(totp.at(now + offset))
                auth_payload = {
                    "UserName": user_id,
                    "Password": pwd_hash,
                    "PAN_DOB": code,
                    "App": "",
                    "ClientID": "",
                    "Key": "",
                    "APIKey": api_key,
                    "Sid": sid,
                    "Override": "Y",
                    "Source": "AUTHPAGE",
                    "Rd": ""
                }

                r2 = session.post("https://authapi.flattrade.in/ftauth", json=auth_payload, timeout=15)
                res2 = r2.json() if r2.status_code == 200 else {}
                redirect_url = res2.get("RedirectURL")
                emsg = res2.get("emsg")

                if redirect_url and not emsg:
                    auth_success = True
                    break
                else:
                    last_emsg = emsg or f"HTTP {r2.status_code}"

            if not auth_success or not redirect_url:
                return None, f"FlatTrade authentication failed: {last_emsg}"

            # Step 4: Extract request_code from redirect URL
            parsed = urlparse(redirect_url)
            qs = parse_qs(parsed.query)
            request_code = qs.get("code", [None])[0] or qs.get("auth_code", [None])[0] or qs.get("request_code", [None])[0]

            if not request_code and parsed.fragment:
                fqs = parse_qs(parsed.fragment)
                request_code = fqs.get("code", [None])[0] or fqs.get("auth_code", [None])[0] or fqs.get("request_code", [None])[0]

            if not request_code:
                return None, "Authorization request code missing in FlatTrade redirect URL"

            # Step 5: Exchange Request Code for Session Token
            raw_hash_input = f"{api_key}{request_code}{api_secret}"
            api_secret_hash = hashlib.sha256(raw_hash_input.encode("utf-8")).hexdigest()

            token_payload = {
                "api_key": api_key,
                "request_code": request_code,
                "api_secret": api_secret_hash
            }

            r3 = session.post("https://authapi.flattrade.in/trade/apitoken", json=token_payload, timeout=15)
            res3 = r3.json() if r3.status_code == 200 else {}

            if res3.get("stat") == "Ok" and res3.get("token"):
                token = res3["token"]
                save_token(token, user_id)
                log.info(f"Automated FlatTrade login SUCCESSFUL for user {user_id}")
                return token, None
            else:
                return None, f"Token exchange error: {res3.get('emsg') or r3.text}"

        except Exception as e:
            log.error(f"FlatTrade automated login exception: {e}")
            return None, str(e)


def get_flattrade_token() -> str | None:
    """
    Returns a valid FlatTrade session token.
    Loads from cache if valid today; otherwise automatically performs headless login.
    """
    cached = get_cached_token()
    if cached:
        return cached

    token, err = generate_flattrade_access_token()
    if token:
        return token
    else:
        log.error(f"Failed to auto-authenticate FlatTrade: {err}")
        return None
