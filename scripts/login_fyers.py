"""
scripts/login_fyers.py — Official FYERS API v3 OAuth Token Generator
Bypasses Cloudflare automated rate limiting by generating a valid daily access token via standard browser authentication.
Saves token to instance/fyers_token.json for use by Options-Desk.
"""

import os
import sys
import json
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import Config, BASE_DIR
from fyers_apiv3 import fyersModel

IST = ZoneInfo("Asia/Kolkata")
TOKEN_FILE = os.path.join(Config.INSTANCE_DIR, "fyers_token.json")


def main():
    print("=" * 65)
    print("  OPTIONS DESK — FYERS API v3 AUTHENTICATION HELPER")
    print("=" * 65)

    client_id = f"{Config.FYERS_APP_ID}-{Config.FYERS_APP_TYPE}"
    secret_key = Config.FYERS_APP_SECRET
    redirect_uri = Config.FYERS_REDIRECT_URI

    if not Config.FYERS_APP_ID or not secret_key:
        print("[!] Missing FYERS_APP_ID or APP_SECRET in config/.env")
        return

    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )

    auth_url = session.generate_authcode()
    print(f"\n1. Open this URL in your browser and log in to FYERS:")
    print("-" * 65)
    print(auth_url)
    print("-" * 65)

    print("\n2. After logging in, you will be redirected to your redirect_uri.")
    print("   Example: http://127.0.0.1:5001/?s=ok&code=200&auth_code=eyJ...")
    redirected_url = input("\nPaste the full redirected URL (or auth_code) here: ").strip()

    if not redirected_url:
        print("[!] No input provided. Exiting.")
        return

    # Extract auth_code from URL if full URL is pasted
    if "auth_code=" in redirected_url:
        parsed = urllib.parse.urlparse(redirected_url)
        params = urllib.parse.parse_qs(parsed.query)
        auth_code = params.get("auth_code", [""])[0]
    else:
        auth_code = redirected_url

    if not auth_code:
        print("[!] Could not parse auth_code. Exiting.")
        return

    print("\nExchanging auth_code for daily access token...")
    session.set_token(auth_code)
    response = session.generate_token()

    if isinstance(response, dict) and response.get("s") == "ok":
        token = response.get("access_token")
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        os.makedirs(Config.INSTANCE_DIR, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump({"date": today_str, "access_token": token}, f)

        print("\n[SUCCESS] Access token obtained and cached to instance/fyers_token.json!")
        print("Live FYERS data will now be used automatically by Options-Desk.")
    else:
        print(f"\n[ERROR] Failed generating token: {response}")


if __name__ == "__main__":
    main()
