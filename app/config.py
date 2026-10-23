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
    STRIKE_STEP = 50.0

    # Authoritative active contract lot size
    from app.contracts import ContractRegistry
    LOT_SIZE: int = ContractRegistry.get_authoritative_lot_size("NIFTY")

    @classmethod
    def get_lot_size(cls, as_of_date=None) -> int:
        """Returns the authoritative exchange lot size active for the target date."""
        from app.contracts import ContractRegistry
        return ContractRegistry.get_authoritative_lot_size(cls.SYMBOL, as_of_date)

    # Risk Management & Capital Defaults (Paper Trading Simulation)
    TOTAL_ACCOUNT_EQUITY = 1000000.0  # ₹10,00,000 INR simulated paper capital
    INTRADAY_CAPITAL_ALLOCATION = 500000.0   # 50% Intraday
    POSITIONAL_CAPITAL_ALLOCATION = 500000.0 # 50% Positional

    # Hard Risk Ceilings (Percentages)
    MAX_RISK_PER_TRADE_PCT = 0.5            # 0.5% max risk per trade
    MAX_COMBINED_PORTFOLIO_RISK_PCT = 2.0   # 2.0% max combined open risk
    MAX_DAILY_LOSS_PCT = 1.5                # 1.5% daily drawdown circuit breaker
    MAX_INTRADAY_POSITIONS = 2              # Max concurrent intraday positions
    MAX_POSITIONAL_POSITIONS = 2            # Max concurrent positional positions
    CONSECUTIVE_LOSS_COOLDOWN_MINS = 15     # 15 min cooldown after stop loss

    # Derived Monetary Risk Ceilings (Authoritative INR Amounts)
    @classmethod
    def get_max_risk_per_trade_inr(cls) -> float:
        return (cls.MAX_RISK_PER_TRADE_PCT / 100.0) * cls.TOTAL_ACCOUNT_EQUITY

    @classmethod
    def get_max_combined_portfolio_risk_inr(cls) -> float:
        return (cls.MAX_COMBINED_PORTFOLIO_RISK_PCT / 100.0) * cls.TOTAL_ACCOUNT_EQUITY

    @classmethod
    def get_max_portfolio_risk_inr(cls) -> float:
        return cls.get_max_combined_portfolio_risk_inr()

    @classmethod
    def get_max_daily_loss_inr(cls) -> float:
        return (cls.MAX_DAILY_LOSS_PCT / 100.0) * cls.TOTAL_ACCOUNT_EQUITY

    @classmethod
    def get_daily_loss_limit_inr(cls) -> float:
        return cls.get_max_daily_loss_inr()

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

    # Statutory NSE Charges Base Rates
    BROKERAGE_PER_ORDER = 20.0
    EXCHANGE_TURNOVER_PCT = 0.0005          # 0.05% of premium turnover
    SEBI_TURNOVER_PCT = 0.000001           # ₹10 per crore (0.0001%)
    STAMP_DUTY_BUY_PCT = 0.00003           # 0.003% on buy turnover
    GST_PCT = 0.18                         # 18% on (Brokerage + Exchange fees)

    # Auto-Trading Control
    AUTO_TRADE_ENABLED: bool = False


