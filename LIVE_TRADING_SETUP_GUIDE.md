# Automated Live Trading Setup & Deployment Guide
**Options Desk — Automated Algorithmic Trading with FYERS API v3**

---

## 1. Executive Summary & Architecture

This guide outlines the exact requirements, risk controls, credentials, and step-by-step procedure required to transition from **Paper Forward-Testing** to **Automated Live Order Execution**.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           OPTIONS DESK SYSTEM                               │
│                                                                             │
│   ┌─────────────────────┐       ┌──────────────────────┐                    │
│   │ Live Option Chain   │ ───>  │ Analytical Engine    │                    │
│   │ (FYERS Data API v3) │       │ (Bias + 6 Pillars)   │                    │
│   └─────────────────────┘       └──────────┬───────────┘                    │
│                                            │                                │
│                                            ▼                                │
│                                 ┌──────────────────────┐                    │
│                                 │ Risk & Safety Gates  │                    │
│                                 │ - Max 2-3 Trades/Day │                    │
│                                 │ - Daily Profit Lock  │                    │
│                                 │ - 3-5 pt Max SL      │                    │
│                                 │ - No Active Trade    │                    │
│                                 └──────────┬───────────┘                    │
│                                            │                                │
│                                            ▼                                │
│                                 ┌──────────────────────┐                    │
│                                 │ Order Bridge         │                    │
│                                 │ (FYERS Order API v3) │                    │
│                                 └──────────────────────┘                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Requirements & Prerequisites Checklist

### A. Broker Account & API Permissions (FYERS)
- [ ] **FYERS Trading Account**: Active account with F&O (Futures & Options) segment enabled.
- [ ] **FYERS API v3 App Registration**:
  - Visit the [FYERS API Dashboard](https://myapi.fyers.in/).
  - Create a new app (App Type: `100` - Web / Personal).
  - Obtain **`APP_ID`** (e.g. `MXPA3JHTVP-100`) and **`APP_SECRET`**.
  - Set **`REDIRECT_URI`** to `http://127.0.0.1:5001/` or your production domain.
- [ ] **2FA TOTP Automation Key**:
  - Enable TOTP on FYERS and store the 32-character base32 TOTP secret key for headless morning authentication without manual OTP entry.
- [ ] **Available Capital**:
  - Minimum allocated margin (e.g., ₹20,000 – ₹50,000 for 1–2 lots of NIFTY ATM option buying).

---

## 3. Mandatory Risk & Safety Rules

Before enabling real capital execution, the following safety gates must be strictly active:

| Safety Gate | Configured Parameter | Description / Enforcement |
| :--- | :--- | :--- |
| **Max Trades Per Day** | `MAX_DAILY_TRADES = 3` | Hard-stop after 3 executed trades in a single session. |
| **Daily Profit Lock** | `DAILY_PROFIT_LOCK_PCT = 6.0%` | If cumulative day's gain reaches 4%–8%, trading stops immediately to lock in daily profits. |
| **Max Risk per Trade** | `NIFTY_MAX_OPTION_RISK_POINTS = 5.0` | Maximum stop loss is strictly capped at **3.0 to 5.0 points** on the option premium. |
| **Single Active Trade** | `SINGLE_ACTIVE_SIGNAL = True` | Never opens a second position while a trade is running. |
| **Max Daily Loss Limit** | `MAX_DAILY_LOSS_INR = ₹1,500` | Circuit breaker: Halts trading immediately if daily drawdown limit is hit. |
| **Time Window Filter** | `09:30 IST to 14:00 IST` | No fresh buying in opening chop (09:15–09:30) or late decay chop (after 14:00). |

---

## 4. Configuration Setup (`config/.env`)

Ensure your `config/.env` file contains all required keys:

```ini
# Server Settings
PORT=5001
FLASK_DEBUG=false
SECRET_KEY=your_strong_random_secret_key_here

# FYERS API v3 Credentials
FYERS_ID=XC04484
PIN=1234
APP_ID=MXPA3JHTVP-100
APP_SECRET=your_fyers_app_secret_here
TOTP_KEY=your_base32_totp_secret_key_here
REDIRECT_URI=http://127.0.0.1:5001/

# Trading Mode
LIVE_TRADING_ENABLED=true
TRADING_SYMBOL=NIFTY
DEFAULT_LOTS=1
MAX_DAILY_TRADES=3
MAX_DAILY_LOSS_INR=1500
DAILY_PROFIT_LOCK_PCT=6.0
NIFTY_MAX_OPTION_RISK_POINTS=5.0
```

---

## 5. Automated Daily Authentication Setup

FYERS API v3 requires an access token generated daily. To run fully automated without manual morning logins:

### Automated Morning Cron (08:30 AM IST)
Set up a daily cronjob or scheduled task to run the token generator script before market open:

```bash
# Windows Task Scheduler or Linux Cron (Runs Mon-Fri at 08:30 IST)
30 8 * * 1-5 cd /path/to/Options-Desk && python -m app.services.fyers_auth_service
```

The authentication module uses `FYERS_ID`, `PIN`, `APP_ID`, `APP_SECRET`, and `TOTP_KEY` to retrieve a fresh `access_token` valid for 24 hours.

---

## 6. Order Placement & Execution Logic

When `LIVE_TRADING_ENABLED=true`:

1. **Entry Order**:
   - Order Type: `LIMIT` order placed at current **Ask price** (with maximum 0.50 pt allowable slippage buffer) to ensure instant fill without market order slippage.
   - Product Type: `INTRADAY` (`BO` Bracket Order or `MARGIN`/`INTRADAY` with server-side SL/TSL monitor).
2. **Stop Loss & Target Management**:
   - Automated server-side tick monitor checks tick-by-tick quotes every second.
   - When Target 1 (+6–10 pts) is reached, Stop Loss automatically trails to Cost (`entry_price`).
   - When Target 2 (+12–18 pts) is reached or Trailing Stop is breached, an immediate exit order is placed at current **Bid price**.
3. **EOD Auto-Squareoff**:
   - Any open position still active at **15:15 IST** is automatically squared off before market close.

---

## 7. Pre-Flight Checklist (On Go-Live Day)

Before 09:15 AM on the first live trading day:

1. **Token Verification**: Verify that `access_token` is valid by checking `/api/options-desk/data`.
2. **Fund Check**: Confirm available margin in FYERS trading account.
3. **System Clock**: Verify system clock is synchronized to Indian Standard Time (IST) via NTP.
4. **Logs Monitoring**: Open terminal logs to monitor live order events:
   ```bash
   tail -f fyersRequests.log
   ```
5. **Kill Switch Readiness**: Keep the manual "Emergency Square-Off" button or broker mobile app ready for manual intervention if needed.

---

## 8. Summary of Steps to Go Live

| Step | Action | Status |
| :---: | :--- | :---: |
| **1** | Complete 3–5 days of paper trade forward-testing. | 🟡 In Progress |
| **2** | Review statistical report & win rate in Strategy Review tab. | ⏳ Pending Review |
| **3** | Input FYERS live credentials (`APP_SECRET`, `TOTP_KEY`) into `.env`. | ⏳ Pending Setup |
| **4** | Test 1-lot live order placement in off-market mock or small contract. | ⏳ Pending Dry Run |
| **5** | Switch `LIVE_TRADING_ENABLED=true` and start automated trading. | 🚀 Ready for Launch |
