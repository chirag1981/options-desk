# Architecture Documentation &bull; NIFTY Options Selling Desk

## 1. Overview & Compact Design Principles
The **NIFTY Options Selling Desk** is designed as a focused, lightweight, and maintainable Flask-based paper-trading platform. It eliminates bloated enterprise abstractions, microservices, and external messaging queues in favor of clean, deterministic Python modules and SQLite persistence.

```text
nifty-options-desk/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── config.py            # Configuration & FYERS .env loading
│   ├── extensions.py        # Shared SQLAlchemy instance
│   ├── models.py            # SQLite data models (Snapshots, Candidates, Positions)
│   ├── routes.py            # Dashboard routes & REST API endpoints
│   ├── market_data.py       # FYERS v3 TOTP login & Option Chain normalization
│   ├── analysis.py          # OI walls, PCR, and Market Regime analysis
│   ├── strategies.py        # 4 defined-risk strategies, Intraday & Positional engines
│   ├── risk.py              # PortfolioRiskManager (0.5% trade cap, 2.0% ceiling)
│   ├── trading.py           # Paper execution simulator, MTM, & lifecycle management
│   ├── performance.py       # Journal metrics, CSV export, & diagnostics
│   ├── scheduler.py         # Dedicated background market poller
│   ├── templates/           # 6 clean Jinja templates (base, dashboard, journal, etc.)
│   └── static/              # Vanilla CSS & focused JavaScript controllers
├── tests/                   # Test suites (analysis, strategies, risk, trading, api)
├── docs/                    # Architecture, Strategy rules, and Risk policy docs
└── run.py                   # Single executable server entry point
```

## 2. Key Modules & Responsibilities
- **`app/config.py`**: Prioritized environment configuration loading (`config/.env` and `.env`). Manages capital allocations, per-trade caps, strike widths, and statutory fees.
- **`app/market_data.py`**: Zero-click automated TOTP login with daily token caching. Quotes normalization for live FYERS feeds and validated simulation fallback.
- **`app/analysis.py`**: Analyzes option chain for S1/S2 Put walls, R1/R2 Call walls, PCR ratio, net $\Delta$OI flow, and classifies market regimes (`TRENDING_BULLISH`, `TRENDING_BEARISH`, `BALANCED_RANGE`, `CHOPPY`).
- **`app/strategies.py`**: Builds defined-risk credit spreads using quote-side pricing (shorts at Bid, longs at Ask). Houses `IntradayEngine` and `PositionalEngine`.
- **`app/risk.py`**: Authoritative risk arbiter validating all candidate orders before paper execution.
- **`app/trading.py`**: Manages position lifecycle, MTM valuation, stop-loss and profit-target checks, and statutory NSE transaction fees.
- **`app/performance.py`**: Aggregates win rate, profit factor, gross/net P&L, and provides sanitized CSV exports.
