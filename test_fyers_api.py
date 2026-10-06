"""
test_fyers_api.py — Verify Fyers automated authentication and optionchain endpoint
"""

import os
import json
import urllib.parse
import requests
import pyotp
import dotenv
from fyers_apiv3 import fyersModel

dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "config", ".env"))

def run_test_login():
    fy_id = os.getenv("FYERS_ID", "").strip(" \"'")
    pin = os.getenv("PIN", "").strip(" \"'")
    app_id = os.getenv("APP_ID", "").strip(" \"'")
    app_type = os.getenv("APP_TYPE", "100").strip(" \"'")
    app_secret = os.getenv("APP_SECRET", "").strip(" \"'")
    totp_key = os.getenv("TOTP_KEY", "").strip(" \"'")
    redirect_uri = os.getenv("REDIRECT_URI", "http://127.0.0.1:5000/").strip(" \"'")
    client_id = f"{app_id}-{app_type}"

    print("============================================================")
    print("TESTING FYERS API LOGIN & CONNECTION")
    print("============================================================")
    print(f"Fyers ID: {fy_id}")
    print(f"Client ID: {client_id}")
    print(f"Redirect URI: {redirect_uri}")

    # Step 1: Send OTP
    r1 = requests.post("https://api-t2.fyers.in/vagator/v2/send_login_otp", json={"fy_id": fy_id, "app_id": "2"}, timeout=10)
    print("Step 1 (send_login_otp):", r1.status_code, r1.json().get("message", ""))
    req_key = r1.json().get("request_key")
    if not req_key:
        print("Failed at Step 1:", r1.text)
        return None

    # Step 2: Verify TOTP
    totp_val = pyotp.TOTP(totp_key).now()
    r2 = requests.post("https://api-t2.fyers.in/vagator/v2/verify_otp", json={"request_key": req_key, "otp": str(totp_val)}, timeout=10)
    print("Step 2 (verify_otp):", r2.status_code, r2.json().get("message", ""))
    req_key2 = r2.json().get("request_key")
    if not req_key2:
        print("Failed at Step 2:", r2.text)
        return None

    # Step 3: Verify PIN
    r3 = requests.post("https://api-t2.fyers.in/vagator/v2/verify_pin", json={"request_key": req_key2, "identity_type": "pin", "identifier": pin}, timeout=10)
    print("Step 3 (verify_pin):", r3.status_code, r3.json().get("message", ""))
    r3_data = r3.json().get("data", {})
    bearer_token = r3_data.get("access_token") or r3_data.get("token_result", {}).get("access_token")
    if not bearer_token:
        print("Failed at Step 3:", r3.text)
        return None

    # Step 4: Request Auth Code
    headers = {"Authorization": f"Bearer {bearer_token}"}
    payload = {
        "fyers_id": fy_id,
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "appType": app_type,
        "code_challenge": "",
        "state": "sample_state",
        "scope": "",
        "nonce": "",
        "response_type": "code",
        "create_cookie": True
    }
    r4 = requests.post("https://api-t1.fyers.in/api/v3/token", headers=headers, json=payload, timeout=10)
    print("Step 4 (get auth_code URL):", r4.status_code)
    auth_url = r4.json().get("Url")
    if not auth_url:
        print("Failed at Step 4:", r4.text)
        return None

    parsed = urllib.parse.urlparse(auth_url)
    qs = urllib.parse.parse_qs(parsed.query)
    auth_code = qs.get("auth_code", [""])[0]
    print(f"Auth code obtained: {auth_code[:15]}...")

    # Step 5: Generate Access Token
    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=app_secret,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )
    session.set_token(auth_code)
    gen_token_resp = session.generate_token()
    print("Step 5 (generate_token):", gen_token_resp.get("s"), gen_token_resp.get("message", ""))
    access_token = gen_token_resp.get("access_token")

    if not access_token:
        print("Failed to generate access_token:", gen_token_resp)
        return None

    print(f"[SUCCESS] Access Token: {access_token[:20]}...")

    # Test Fyers Profile & Option Chain
    fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token=access_token, log_path="")
    prof = fyers.get_profile()
    print("Profile:", prof.get("s"), prof.get("data", {}).get("name"))

    print("\nTesting Fyers Option Chain endpoint for NSE:NIFTY50-INDEX...")
    oc_data = {
        "symbol": "NSE:NIFTY50-INDEX",
        "strikecount": 10,
    }
    oc_resp = fyers.optionchain(data=oc_data)
    print("Option Chain status:", oc_resp.get("s"), oc_resp.get("message", ""))
    options_chain = oc_resp.get("data", {}).get("optionsChain", [])
    print(f"Total option contracts returned in 1 single call: {len(options_chain)}")
    if options_chain:
        print("Sample contract:", options_chain[0])

    return access_token

if __name__ == "__main__":
    run_test_login()
