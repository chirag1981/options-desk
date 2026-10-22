"""
app/config.py — Central Application and Engine Configuration
Loads environment variables from config/.env and .env.
Defines settings for Flask, FYERS API v3, trading engines, risk limits, and paper execution.
"""

import os
from zoneinfo import ZoneInfo
import dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_ENV_PATH = os.path.join(BASE_DIR, "config", ".env")
ROOT_ENV_PATH = os.path.join(BASE_DIR, ".env")

# Priority loading: config/.env then root .env
if os.path.exists(CONFIG_ENV_PATH):
    dotenv.load_dotenv(CONFIG_ENV_PATH)
if os.path.exists(ROOT_ENV_PATH):
    dotenv.load_dotenv(ROOT_ENV_PATH)

IST = ZoneInfo("Asia/Kolkata")


class Config:
    """Base application and trading configuration."""
    SECRET_KEY = os.environ.get("SECRET_KEY", "nifty-options-desk-secret-key-2026")
    INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
    os.makedirs(INSTANCE_DIR, exist_ok=True)

    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(INSTANCE_DIR, 'options_desk.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # FYERS API v3 Credentials (loaded securely from config/.env)
    FYERS_ID = os.environ.get("FYERS_ID", "").strip("\"' \t\r\n")
    FYERS_APP_ID = os.environ.get("APP_ID", "").strip("\"' \t\r\n")
    FYERS_APP_TYPE = os.environ.get("APP_TYPE", "100").strip("\"' \t\r\n")
    FYERS_APP_SECRET = os.environ.get("APP_SECRET", "").strip("\"' \t\r\n")
    FYERS_TOTP_KEY = os.environ.get("TOTP_KEY", "").strip("\"' \t\r\n")
    FYERS_PIN = os.environ.get("PIN", "").strip("\"' \t\r\n")
    FYERS_REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://127.0.0.1:5001/").strip("\"' \t\r\n")
    FYERS_ACCESS_TOKEN = os.environ.get("FYERS_ACCESS_TOKEN", "").strip("\"' \t\r\n")

    # Underlyings & Index Configuration
    SYMBOL = "NIFTY"
    INDEX_FYERS_SYMBOL = "NSE:NIFTY50-INDEX"
    LOT_SIZE = 65
    STRIKE_STEP = 50.0

    # Risk Management & Capital Defaults (Paper Trading)
    TOTAL_ACCOUNT_EQUITY = 1000000.0  # 10 Lakh INR paper capital
    INTRADAY_CAPITAL_ALLOCATION = 500000.0   # 50% Intraday
    POSITIONAL_CAPITAL_ALLOCATION = 500000.0 # 50% Positional

    # Hard Risk Ceilings
    MAX_RISK_PER_TRADE_PCT = 0.5            # 0.5% max risk per trade
    MAX_COMBINED_PORTFOLIO_RISK_PCT = 2.0   # 2.0% max combined open risk
    MAX_DAILY_LOSS_PCT = 1.5                # 1.5% daily drawdown circuit breaker
    MAX_INTRADAY_POSITIONS = 2              # Max concurrent intraday positions
    MAX_POSITIONAL_POSITIONS = 2            # Max concurrent positional positions
    CONSECUTIVE_LOSS_COOLDOWN_MINS = 15     # 15 min cooldown after stop loss

    # Spread Quality Filters
    MIN_NET_CREDIT_PTS = 10.0               # Minimum net credit for 50-wide spread
    MIN_CREDIT_SPREAD_PCT = 18.0            # Net credit must be >= 18% of spread width
    MIN_SPOT_CUSHION_PTS = 20.0             # Short strike must be >= 20 pts from spot
    MAX_BID_ASK_SPREAD_PCT = 10.0           # Max allowable leg bid/ask spread

    # Engine Policies
    INTRADAY_ENTRY_START = "09:20:00"
    INTRADAY_ENTRY_CUTOFF = "15:00:00"
    INTRADAY_SESSION_CLOSE_TIME = "15:20:00"
    POSITIONAL_PRE_EXPIRY_EXIT_TIME = "14:30:00"  # Expiry day exit

    # Statutory NSE Charges (per lot basis)
    BROKERAGE_PER_ORDER = 20.0
    STT_SELL_PCT = 0.00125                  # 0.125% on option sell premium
    EXCHANGE_TURNOVER_PCT = 0.0005          # 0.05%
    SEBI_TURNOVER_PCT = 0.000001
    STAMP_DUTY_BUY_PCT = 0.00003
    GST_PCT = 0.18                          # 18% on brokerage & transaction charges

    # Auto-Trading Control
    AUTO_TRADE_ENABLED: bool = False

