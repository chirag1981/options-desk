"""
test_flattrade_api.py — Verify Flattrade API Connection & Account Limits
Tests the generated session token against Flattrade REST API endpoints.
"""

import os
import json
import logging
from datetime import datetime
import requests
import dotenv

# Load environment variables
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "config", ".env"))
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_flattrade")

USER_ID = (os.getenv("FLATTRADE_USER_ID") or "").strip()
TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), "instance", "flattrade_token.json")
BASE_URL = "https://piconnect.flattrade.in/PiConnectAPI"


def load_token() -> str | None:
    """Loads session token from instance/flattrade_token.json."""
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("token")
    except Exception as e:
        log.error(f"Error reading token file: {e}")
        return None


def get_limits(token: str, user_id: str):
    """Fetches user account margin limits."""
    url = f"{BASE_URL}/Limits"
    # Flattrade PiConnect payload format: jData={"uid": "...", "actid": "..."} & jKey=token
    j_data = json.dumps({"uid": user_id, "actid": user_id})
    payload = f"jData={j_data}&jKey={token}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(url, data=payload, headers=headers, timeout=10)
    return resp.json()


def get_user_details(token: str, user_id: str):
    """Fetches user details."""
    url = f"{BASE_URL}/UserDetails"
    j_data = json.dumps({"uid": user_id})
    payload = f"jData={j_data}&jKey={token}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(url, data=payload, headers=headers, timeout=10)
    return resp.json()


def main():
    print("=" * 60)
    print("TESTING FLATTRADE API CONNECTION")
    print("=" * 60)

    token = load_token()
    if not token:
        print("[ERROR] No token found in instance/flattrade_token.json.")
        print("Please run `python get_flattrade_token.py` first to generate a token.")
        return

    user_id = USER_ID
    if not user_id:
        print("[ERROR] FLATTRADE_USER_ID not found in config/.env.")
        return

    print(f"User ID: {user_id}")
    print(f"Token:   {token[:8]}...{token[-6:]}")
    print("\n1. Fetching User Details...")
    try:
        details = get_user_details(token, user_id)
        print(f"Response: {json.dumps(details, indent=2)}")
    except Exception as e:
        print(f"Error fetching User Details: {e}")

    print("\n2. Fetching Account Limits (Funds & Margins)...")
    try:
        limits = get_limits(token, user_id)
        print(f"Response: {json.dumps(limits, indent=2)}")
        if limits.get("stat") == "Ok":
            print("\n[SUCCESS] API connection and token verification SUCCESSFUL!")
        else:
            print(f"\n[NOTE] API response status: {limits.get('stat')}, emsg: {limits.get('emsg')}")
    except Exception as e:
        print(f"Error fetching limits: {e}")

    print("=" * 60)


if __name__ == "__main__":
    main()
