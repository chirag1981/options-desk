"""
get_flattrade_token.py — Flattrade Daily Token Generator
Reads credentials from config/.env, spins up a local redirect server on port 8080,
launches browser authentication, exchanges the authorization code for an API token,
and saves the valid session token to instance/flattrade_token.json.
"""

import os
import sys
import json
import hashlib
import webbrowser
import logging
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import dotenv
import pyotp
import re
import threading
import time

# Load environment variables
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "config", ".env"))
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("flattrade_token")

API_KEY = (os.getenv("FLATTRADE_API_KEY") or "").strip()
API_SECRET = (os.getenv("FLATTRADE_API_SECRET") or "").strip()
USER_ID = (os.getenv("FLATTRADE_USER_ID") or "").strip()
TOTP_KEY = (os.getenv("FLATTRADE_TOTP_KEY") or "").strip()
PORT = int(os.getenv("FLATTRADE_PORT", 8080))
REDIRECT_URI = os.getenv("FLATTRADE_REDIRECT_URI", f"http://localhost:{PORT}")

TOKEN_CACHE_DIR = os.path.join(os.path.dirname(__file__), "instance")
TOKEN_CACHE_FILE = os.path.join(TOKEN_CACHE_DIR, "flattrade_token.json")

AUTH_URL = f"https://auth.flattrade.in/?app_key={API_KEY}"
TOKEN_ENDPOINT = "https://authapi.flattrade.in/trade/apitoken"


class FlattradeAuthHandler(BaseHTTPRequestHandler):
    auth_code = None

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        code = qs.get("code", [None])[0] or qs.get("auth_code", [None])[0] or qs.get("request_code", [None])[0]

        if code:
            FlattradeAuthHandler.auth_code = code.strip()
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Flattrade Authentication Success</title>
                <style>
                    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
                    .card { background: #1e293b; padding: 2.5rem; border-radius: 12px; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5); text-align: center; max-width: 480px; border: 1px solid #334155; }
                    h1 { color: #10b981; font-size: 1.6rem; margin-bottom: 0.5rem; }
                    p { color: #94a3b8; font-size: 0.95rem; line-height: 1.5; }
                    .badge { display: inline-block; background: #065f46; color: #6ee7b7; padding: 0.35rem 0.8rem; border-radius: 9999px; font-weight: 600; font-size: 0.85rem; margin-top: 1rem; }
                </style>
            </head>
            <body>
                <div class="card">
                    <h1>Authentication Successful!</h1>
                    <p>Authorization code received. Generating your API token...</p>
                    <div class="badge">You can close this tab now.</div>
                </div>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>Redirect received.</h1>")

    def log_message(self, format, *args):
        pass


def exchange_code_for_token(request_code: str) -> dict:
    """Computes SHA256 of (api_key + request_code + api_secret) and requests session token."""
    raw_hash_input = f"{API_KEY}{request_code}{API_SECRET}"
    api_secret_hash = hashlib.sha256(raw_hash_input.encode("utf-8")).hexdigest()

    payload = {
        "api_key": API_KEY,
        "request_code": request_code,
        "api_secret": api_secret_hash
    }

    log.info("Sending token exchange request to Flattrade...")
    try:
        resp = requests.post(TOKEN_ENDPOINT, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        try:
            return resp.json()
        except Exception:
            return {"stat": "Not_Ok", "emsg": resp.text}
    except Exception as e:
        return {"stat": "Not_Ok", "emsg": str(e)}


def save_token(token: str) -> None:
    """Saves session token to instance/flattrade_token.json with timestamp."""
    os.makedirs(TOKEN_CACHE_DIR, exist_ok=True)
    data = {
        "user_id": USER_ID,
        "token": token,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "created_at": datetime.now().isoformat()
    }
    with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info(f"Token saved successfully to: {TOKEN_CACHE_FILE}")


def generate_token():
    print("=" * 60)
    print("FLATTRADE API TOKEN GENERATOR (AUTOMATED HEADLESS)")
    print("=" * 60)

    if not API_KEY or not API_SECRET:
        log.error("Missing FLATTRADE_API_KEY or FLATTRADE_API_SECRET in config/.env")
        return None

    # Step 1: Attempt Instant Automated Headless Login (Zero-Click)
    if USER_ID and TOTP_KEY and os.getenv("FLATTRADE_PASSWORD"):
        print("\n[INFO] Attempting instant automated zero-click headless login...")
        try:
            from app.services.flattrade_auth import generate_flattrade_access_token
            token, err = generate_flattrade_access_token()
            if token:
                print("\n" + "=" * 60)
                print("[SUCCESS] Automated Headless FlatTrade login SUCCESSFUL!")
                print(f"Token: {token[:8]}...{token[-6:]}")
                print("Saved to: instance/flattrade_token.json")
                print("Valid until: 5:00 AM IST tomorrow")
                print("=" * 60 + "\n")
                return token
            else:
                print(f"[NOTE] Headless login returned: {err}. Falling back to browser...")
        except Exception as e:
            print(f"[NOTE] Automated headless failed: {e}. Falling back to browser...")

    print(f"\n[1] User ID:      {USER_ID or 'Not set'}")
    print(f"[2] Redirect URI: {REDIRECT_URI}")
    print(f"[3] Port:         {PORT}")

    if TOTP_KEY:
        try:
            totp = pyotp.TOTP(TOTP_KEY)
            current_totp = totp.now()
            seconds_left = 30 - (int(time.time()) % 30)
            print(f"[4] Current TOTP: {current_totp} (valid for {seconds_left}s)")
        except Exception:
            pass

    FlattradeAuthHandler.auth_code = None

    # Bind on 0.0.0.0 to listen on both localhost and 127.0.0.1
    server = None
    try:
        server = HTTPServer(("0.0.0.0", PORT), FlattradeAuthHandler)
        server.timeout = 0.5
        server_thread = threading.Thread(target=_run_server_loop, args=(server,), daemon=True)
        server_thread.start()
        print(f"[5] Listener:     http://127.0.0.1:{PORT} & http://localhost:{PORT}")
    except OSError as e:
        print(f"[5] Listener Note: {e}")

    print("\n" + "-" * 60)
    print("OPEN THIS URL IN YOUR BROWSER:")
    print(AUTH_URL)
    print("-" * 60)

    try:
        webbrowser.open(AUTH_URL)
    except Exception:
        pass

    print("\nWaiting for login and redirect (Press Ctrl+C to cancel)...")
    start_time = time.time()
    try:
        while FlattradeAuthHandler.auth_code is None:
            time.sleep(0.5)
            if (time.time() - start_time) > 180:
                print("\n[TIMEOUT] 180 seconds elapsed. Please try again.")
                return None
    except KeyboardInterrupt:
        print("\n[CANCELLED] Interrupted by user.")
        return None

    auth_code = FlattradeAuthHandler.auth_code
    if not auth_code:
        print("\n[ERROR] No authorization code received.")
        return None

    print(f"\n[OK] Captured code: {auth_code[:6]}***")
    token_response = exchange_code_for_token(auth_code)

    if token_response.get("stat") == "Ok" and token_response.get("token"):
        token = token_response["token"]
        save_token(token)
        print("\n" + "=" * 60)
        print("[SUCCESS] Flattrade session token generated successfully!")
        print(f"Token: {token[:8]}...{token[-6:]}")
        print("Saved to: instance/flattrade_token.json")
        print("Valid until: 5:00 AM IST tomorrow")
        print("=" * 60 + "\n")
        return token
    else:
        print(f"\n[ERROR] Token exchange error from Flattrade:")
        print(json.dumps(token_response, indent=2) if isinstance(token_response, dict) else token_response)
        return None


def _run_server_loop(server):
    while FlattradeAuthHandler.auth_code is None:
        try:
            server.handle_request()
        except Exception:
            break


if __name__ == "__main__":
    generate_token()
