# NIFTY Options Selling Desk (Executive Edition)

Autonomous Defined-Risk Options Selling & Paper Trading Platform designed for NIFTY 50 weekly and monthly index options.

Built with Python, Flask, SQLite, and Vanilla CSS/JavaScript, following high-craft financial desktop standards with Warm Brown & Gold studio aesthetics.

---

## Key Capabilities

### 1. Dual-Engine Architecture
- **INTRADAY Engine**:
  - Focuses on current weekly expiry contracts with tight risk boundaries.
  - Requires 5-minute breakout confirmation.
  - Hard cutoff at 15:20 IST with automated market exit.
- **POSITIONAL Engine**:
  - Targets 7–14 DTE contracts beyond major S1/R1 open interest walls.
  - Evaluates daily and weekly trend context with wider safety buffers.

### 2. Supported Defined-Risk Strategies
Only fully hedged credit spreads are permitted (strictly zero naked options allowed):
- **Bull Put Spread**: Credit put spread sold below validated support floor (S1/S2).
- **Bear Call Spread**: Credit call spread sold above validated resistance ceiling (R1/R2).
- **Iron Condor**: 4-legged non-directional delta-neutral credit structure across balanced regimes.
- **Iron Butterfly**: Strategically gated/disabled by risk guard until minimum credit requirements are met.

### 3. Autonomous Strategy Auto-Selection
- Continuously scans spot regime (BALANCED_RANGE, BULLISH_TREND, BEARISH_TREND, CHOPPY_VOLATILE).
- Analyzes Put-Call Ratio (PCR), total writer momentum, and OI delta positioning.
- Auto-selects single or dual strategy setups with autonomous paper execution capabilities.

### 4. Dynamic Capital & Transaction Cost Engine
- **Margin Calculation**: Real-time margin estimation accounting for exchange hedge benefits (e.g. ₹38k–₹42k for spreads, ₹52k–₹62k for Iron Condor).
- **Fee Transparency**: Itemized brokerage, STT, exchange turnover fees, SEBI charges, and GST calculated per leg and order size.
- **Dynamic Lot Sizing**: Live scaling of margin, charges, net credit cashflow, max profit, and max loss as lot counts are adjusted.

### 5. Independent Refresh Cycles
- **Active Open Positions**: Synchronized every **1 Minute** (60,000 ms) with real-time mark-to-market (MTM) P&L, stop-loss and profit target monitoring.
- **Entire Project Data**: Synchronized every **3 Minutes** (180,000 ms) with fresh option chain data, market regime analysis, OI wall shifts, and strategy candidate recalculations.

---

## Quick Start

### 1. Prerequisites
- Python 3.11+
- Virtual environment (recommended)

### 2. Installation
```bash
pip install -r requirements.txt
```

### 3. Run Application
```bash
python run.py
```
Open your browser at `http://127.0.0.1:5001/` to access the dashboard.

### 4. Run Test Suite
```bash
pytest tests/ -v
```

---

## Project Structure
```text
Options-Desk/
├── app/
│   ├── analysis.py          # Market regime, PCR, and OI boundary analyzer
│   ├── config.py            # Environment and risk parameters
│   ├── extensions.py        # SQLAlchemy & Flask extensions
│   ├── market_data.py       # FYERS API client with simulation fallback
│   ├── models.py            # SQLite schema (positions, legs, snapshots)
│   ├── performance.py       # Portfolio win rate, profit factor, MTM P&L
│   ├── risk.py              # Authoritative risk guard and portfolio caps
│   ├── routes.py            # REST endpoints and dashboard controllers
│   ├── scheduler.py         # Background market poller & session lifecycle
│   ├── strategies.py        # Intraday & Positional strike selection engines
│   ├── trading.py           # Paper trade execution and MTM valuation
│   ├── static/              # Vanilla CSS and ES6 JavaScript
│   └── templates/           # Semantic Jinja2 templates (Dashboard, OI, Journal)
├── docs/                    # Architecture, risk policy, and strategy rules
├── scripts/                 # FYERS authentication and utility scripts
├── tests/                   # Pytest test suite (14 passing tests)
├── run.py                   # Application entry point
└── requirements.txt         # Pinned production dependencies
```

---

## Operational Safety & Compliance
- **Paper Trading Only**: All trade placements and position monitoring are strictly simulated in local SQLite storage. No live order placement logic is enabled.
- **Zero Naked Options**: Single-leg naked short calls or short puts are structurally blocked at the engine and risk guard boundaries.
- **Daily Loss Circuit**: Automated circuit breaker halts execution if portfolio drawdown exceeds 1.5% in a single trading session.
