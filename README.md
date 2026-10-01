# 📊 Options Desk — Institutional-Grade Options Analytics & Paper Trading Journal

A high-performance, real-time Options Analytical Terminal and Automated Paper Trading Journal designed for Indian benchmark indices (**NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX**) powered by live FYERS API market data.

---

## 🌟 Key Features

### 1. 🎯 Market Bias & Multi-Factor Scoring Engine
* **Holistic Directional Bias:** Calculates net Bullish vs. Bearish conviction (0–100%) using real-time Open Interest (OI) distribution, Put-Call Ratio (PCR), and Volume expansion.
* **Institutional Key Levels:** Identifies primary and secondary Support (`S1`, `S2`) and Resistance (`R1`, `R2`) levels from weighted OI clusters.
* **Strict Breakout Validation:** Filters false breakouts by requiring spot price confirmation against key structural levels.

---

### 2. ⚡ Automated Signal Tracker & Paper Trading Journal
* **Selective Option Buying Flow:** Triggers high-probability CE / PE trade plans (Entry, SL, T1, T2, R:R) only when setup quality exceeds the threshold.
* **🛡️ P1 — Trailing Stop-Loss to Cost on Target 1:** Stop-Loss is automatically trailed to `Entry + ₹1.0` buffer once Target 1 is achieved (`TSL_HIT`). Winning trades never turn into losses.
* **🎯 P4 — Partial 50% Profit Booking:** Locks in 50% realized gains at Target 1, while allowing the remaining 50% to ride risk-free toward Target 2 (`TARGET_2_HIT` / `🛡️ T1 BOOKED & TSL`).
* **⏱️ P3 — Dynamic Stagnation Time-Stop:** Automatically closes stagnant positions (25 mins morning / 12 mins afternoon) if the trade fails to progress toward Target 1, protecting buyers from Theta decay.
* **⏰ Strict 14:00 (2:00 PM) Cutoff:** Disables fresh option buying in the late afternoon session to eliminate late-day theta crush and squaring-off whipsaws.
* **🔒 Anti-Whipsaw Cooldown:** Enforces a 15-minute lockout per contract after exit to prevent rapid duplicate re-entries.
* **🛡️ SL Slippage Guard:** Bounds Stop-Loss execution to the planned SL level (with max 2% realistic slippage), avoiding artificial losses caused by poll lag.

---

### 3. 🔍 Visual OI Activity & Institutional Positioning
* **Directional Trend Badges:**
  * **Call Writing:** `🔻 Ceiling (Resistance)` — Institutional sellers capping market upside.
  * **Put Writing:** `🔺 Floor (Support)` — Institutional sellers building downside base.
  * **Call Unwinding:** `↗️ Clearing` — Short covering rally, unlocking upward room.
  * **Put Unwinding:** `↘️ Cracking` — Long unwinding, increasing downside risk.
* **Big OI Movements:** Institutional flow sorted by `|ΔOI|` with `▲ +OI` / `▼ -OI` volume indicators and plain-English intent tags (`🔻 Resistance Build`, `🔺 Support Build`, `↗️ Short Covering`, `↘️ Support Cracking`).
* **Interactive Tooltips:** Micro-tooltips explaining the trading implications of every tile on hover.

---

### 4. 📈 High-Density Option Chain & OI Trend
* **OI Trend Table:** Focused view of ATM ± 8 strikes with ITM/OTM color coding, strike-by-strike delta, and writing/unwinding labels.
* **Full Interactive Option Chain:** Complete real-time strike chain with Calls and Puts metrics, live LTP, IV, Volume, and OI changes.

---

## 🛠️ Architecture & Tech Stack

* **Backend:** Python 3.11+, Flask Blueprints, SQLite (WAL mode).
* **Frontend:** Vanilla JavaScript (ES6+), Vanilla CSS with custom properties & design tokens.
* **Data Provider:** FYERS REST API v3 with background polling scheduler (every 3 minutes) & real-time cache.
* **Design Philosophy:** Minimalistic, high-density, accessible dark/gold palette with icon-first visual hierarchy.

```
Options-Desk/
├── app/
│   ├── __init__.py                 # Flask App factory
│   ├── routes/
│   │   ├── __init__.py
│   │   └── options_desk.py         # Desk API endpoints & page routes
│   └── services/
│       ├── fyers_auth.py           # FYERS authentication & token manager
│       ├── fyers_options_service.py# Live option chain fetching & Greeks
│       ├── options_engine.py       # Market Bias, Levels & Scoring Engine
│       ├── options_scheduler.py    # Background automated polling worker
│       └── options_signal_service.py# Trade lifecycle, P&L, TSL & Journal DB
├── config/                         # Configuration settings
├── static/
│   ├── css/
│   │   └── options_desk.css        # Responsive, compact design system
│   └── js/
│       └── options_desk.js         # Reactive DOM controller & Charting
├── templates/
│   ├── base.html                   # Global HTML5 layout
│   └── options_desk.html           # Options Terminal workspace
├── instance/                       # SQLite persistent databases
├── run.py                          # Application entry point
└── README.md                       # Project documentation
```

---

## 🚀 Quick Start Guide

### 1. Prerequisites
* Python 3.10 or higher
* FYERS Trading Account with API access

### 2. Installation
```bash
# Clone the repository
git clone https://github.com/chirag1981/options-desk.git
cd options-desk

# Create and activate virtual environment
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install flask requests python-dotenv
```

### 3. FYERS Configuration
Create or configure your FYERS credentials in your environment or configuration files:
* `APP_ID`: Your FYERS App ID
* `SECRET_KEY`: Your FYERS App Secret
* `REDIRECT_URI`: Registered redirect URL

### 4. Running the Application
```bash
python run.py
```
Open your browser and navigate to:
```
http://localhost:5000/options-desk
```

---

## 📊 Trade Outcome Legend

| Badge | Status | Explanation |
| :--- | :--- | :--- |
| `🎯 TARGET 2 HIT` | `TARGET_2_HIT` | Both Target 1 and Target 2 reached (100% full profit booked). |
| `🛡️ T1 BOOKED & TSL` | `TSL_HIT` | 50% locked at Target 1, remaining 50% exited at Break-Even on pullback. |
| `🎯 TARGET 1 HIT` | `TARGET_1_HIT` | Target 1 reached, 50% profit realized, SL trailed to cost. |
| `⏱ TIME STOP` | `TIME_STOP_EXIT` | Exited due to sideways stagnation to avoid Theta decay. |
| `🛑 SL HIT` | `SL_HIT` | Stop-Loss triggered (bounded to max 2% slippage). |
| `⏱ EOD CLOSED` | `EOD_CLOSED` | Session close square-off at 15:20 IST. |
| `✋ SQUARED OFF` | `MANUALLY_CLOSED` | Trader manually squared off the position. |

---

## 📄 License
This project is proprietary and intended for options analysis and paper trading journal tracking.
