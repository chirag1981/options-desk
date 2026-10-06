# 📊 Options Desk — Institutional-Grade Options Analytics & Paper Trading Journal

A high-performance, real-time Options Analytical Terminal and Automated Paper Trading Journal designed for Indian benchmark indices (**NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX**) powered by live FYERS API v3 market data.

---

## 🌟 Key Features

### 1. 🎯 Market Bias & Multi-Factor Scoring Engine
* **Holistic Directional Bias:** Calculates net Bullish vs. Bearish conviction (0–100%) using real-time Open Interest (OI) distribution, Put-Call Ratio (PCR), and Volume expansion.
* **Institutional Key Levels:** Identifies primary and secondary Support (`S1`, `S2`) and Resistance (`R1`, `R2`) levels from weighted OI clusters.
* **📏 Visual Price Range Ladder:** Real-time visual price track showing spot position relative to Support base ($S_1$) and Resistance ceiling ($R_1$) with expected range points.
* **🧭 Directional Conviction Radar (Under Key Levels):**
  * Synthesizes 3 data points (**Spot vs ATM position**, **Big OI Writing ratio**, and **Channel proximity**) into a single, high-conviction directional vector.
  * Dynamically outputs **`⚡ PURE PE PLAY`** (Bearish Ceiling breakdown), **`⚡ PURE CE PLAY`** (Bullish Floor breakout), or **`⏸ WAIT / TRAP`** (range chop) with actionable playbooks and invalidation levels.

---

### 2. ⚡ Automated Signal Tracker & Paper Trading Journal
* **Selective Option Buying Flow:** Triggers directional CE / PE trade plans (Entry, SL, T1, T2, R:R) upon multi-factor confirmation (Bias Score ≥ 60%, level breakout/breakdown, momentum, real bid/ask, and 2 consecutive cycle confirmations). Setup quality score (0–100) is tracked in telemetry.
* **🛡️ Dynamic Trailing Stop-Loss (TSL):**
  * When profit reaches +10% or 50% to Target 1: Stop-Loss ratchets to `Entry + 1 pt` (Guaranteed Breakeven).
  * When profit reaches +20% or 80% to Target 1: Stop-Loss locks in +10% gain (`Entry × 1.10`).
  * When Target 1 is reached: Status updates to `TARGET_1_HIT` and Stop-Loss ratchets to `Entry + 0.5 × (Target 1 - Entry)`.
  * Beyond Target 1: Stop-Loss ratchets dynamically: `Target 1 + 0.5 × (Highest - Target 1)`.
* **🎯 50% Partial Profit Booking:** Locks in 50% realized gains at Target 1, while allowing the remaining 50% to ride risk-free toward Target 2 (`TARGET_2_HIT` / `🛡️ T1 BOOKED & TSL`).
* **⏱️ Dynamic Stagnation Time-Stop (`TIME_STOP_EXIT`):** Automatically closes stagnant positions (30 mins before 13:00 / 15 mins after 13:00 IST) if the trade fails to progress toward Target 1, protecting buyers from Theta decay.
* **⏰ Strict 14:00 (2:00 PM) Cutoff:** Disables fresh option buying in the late afternoon session to eliminate late-day theta crush and squaring-off whipsaws.
* **🔒 Anti-Whipsaw Cooldown:** Enforces a 15-minute lockout per index after exit to prevent revenge-trading.
* **🛡️ Real Bid Fills & Slippage Guard:** Entries fill strictly at live Ask prices and exits at live Bid prices, ensuring 100% realistic execution logs without mid-market distortion.

---

### 3. 🤖 Autonomous Post-Trade Diagnostic Agent
* **Automated Audit Reviews:** Every closed trade is analyzed tick-by-tick upon exit and categorized into 7 mutually exclusive diagnostic classifications:
  1. `CLEAN_WIN` (Smooth move to targets with minimal drawdown)
  2. `WRONG_DIRECTION` (Failed immediately, MFE ≤ 0.05R)
  3. `RIGHT_THEN_REVERSED` (Made +0.5R or 50% to T1, then reversed)
  4. `STOPPED_BY_NOISE` (Hit SL, but price later recovered to T1 within 30 min)
  5. `THETA_STAGNATION` (Eroded by time decay over >45 min)
  6. `EARLY_EXIT` (Exited early while trade ran 1.5x+ further)
  7. `DATA_QUALITY_ISSUE` (Anomalous quotes/spreads)
* **Strategy Health Scoring & Replay:** Performs counterfactual tick replays and parameter optimization once $\ge 30$ trade samples are collected.

---

### 4. 📈 High-Density Option Chain & OI Trend
* **OI Trend Table:** Focused view of ATM ± 8 strikes with ITM/OTM color coding, strike-by-strike delta, and writing/unwinding labels.
* **Full Interactive Option Chain:** Complete real-time strike chain with Calls and Puts metrics, live LTP, IV, Volume, and OI changes powered in sub-150ms latency by FYERS API v3.

---

## 🛠️ Architecture & Tech Stack

* **Backend:** Python 3.11+, Flask Blueprints, SQLite (WAL mode).
* **Frontend:** Vanilla JavaScript (ES6+), Vanilla CSS with custom properties & design tokens.
* **Data Provider:** FYERS API v3 with single-call native option chain endpoint & real-time cache.
* **Design Philosophy:** Minimalistic, high-density, accessible dark/gold palette with icon-first visual hierarchy.

Detailed architecture specifications and flow diagrams are available in [ARCHITECTURE.md](file:///e:/Stock%20Market%20Top%20Stocks/Options-Desk/ARCHITECTURE.md).

```
Options-Desk/
├── app/
│   ├── __init__.py                 # Flask App factory
│   ├── routes/
│   │   ├── __init__.py
│   │   └── options_desk.py         # Desk API endpoints & page routes
│   └── services/
│       ├── fyers_auth.py           # FYERS headless automated TOTP authentication
│       ├── fyers_options_service.py# Live option chain & FYERS spot data
│       ├── options_engine.py       # Market Bias, Levels & Scoring Engine
│       ├── options_scheduler.py    # Background automated polling worker with exclusive lock
│       ├── options_signal_service.py# Trade lifecycle, P&L, TSL & Journal DB
│       ├── trade_analyzer_agent.py # Automated post-trade diagnostic audit agent
│       └── options_replay_backtest.py# Tick-level backtesting & walk-forward optimization
├── config/                         # Configuration settings (.env)
├── static/
│   ├── css/
│   │   └── options_desk.css        # Responsive, compact design system
│   └── js/
│       └── options_desk.js         # Reactive DOM controller & live charts
├── templates/
│   ├── base.html                   # Global HTML5 layout
│   └── options_desk.html           # Options Terminal workspace
├── instance/                       # SQLite persistent databases & token cache
├── ARCHITECTURE.md                 # System Architecture & Technical Specifications
├── UBUNTU_DEPLOYMENT_GUIDE.txt     # Complete 24/7 Linux Server Deployment Guide
├── requirements.txt                # Python dependencies
├── run.py                          # Application entry point
└── README.md                       # Project documentation
```

---

## 🚀 Quick Start Guide

### 1. Prerequisites
* Python 3.10 or higher
* FYERS Account with API v3 access & TOTP enabled

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
pip install -r requirements.txt
```

### 3. FYERS Configuration
Configure your FYERS credentials in `config/.env`:
```env
PORT=5001
FLASK_DEBUG=false
SECRET_KEY=your_secure_secret_key

FYERS_ID=XC04484
PIN=your_4_digit_pin
APP_ID=MXPA3JHTVP
APP_TYPE=100
APP_SECRET=your_fyers_app_secret
TOTP_KEY=your_totp_secret_key
REDIRECT_URI=http://127.0.0.1:5001/
```

### 4. Running Locally
```bash
python run.py
```
Open your browser and navigate to:
```
http://localhost:5001/options-desk
```

---

## 🐧 Ubuntu Server 24/7 Deployment

For detailed production instructions using `systemd` and `gunicorn`, see [UBUNTU_DEPLOYMENT_GUIDE.txt](file:///e:/Stock%20Market%20Top%20Stocks/Options-Desk/UBUNTU_DEPLOYMENT_GUIDE.txt).

```bash
# Start 24/7 production service with Gunicorn
gunicorn --workers 1 --threads 4 --bind 0.0.0.0:5001 --timeout 120 run:app
```

---

## 📊 Trade Outcome Legend

| Badge | Status | Explanation |
| :--- | :--- | :--- |
| `🎯 TARGET 2 HIT` | `TARGET_2_HIT` | Both Target 1 and Target 2 reached (100% full profit booked). |
| `🛡️ T1 BOOKED & TSL` | `TSL_HIT` | 50% locked at Target 1, remaining 50% exited at Trailed SL on pullback. |
| `🎯 TARGET 1 HIT` | `TARGET_1_HIT` | Target 1 reached, 50% profit realized, SL trailed to cost. |
| `⏱ TIME STOP` | `TIME_STOP_EXIT` | Exited due to sideways stagnation to protect from Theta decay. |
| `🛑 SL HIT` | `STOP_LOSS_HIT` | Stop-Loss triggered (bounded to real Bid price). |
| `⏱ EOD CLOSED` | `EOD_CLOSED` | Session close square-off at 15:20 IST. |
| `✋ SQUARED OFF` | `MANUALLY_CLOSED` | Trader manually squared off the position. |

---

## 📄 License
This project is proprietary and intended for options analysis and paper trading journal tracking.
