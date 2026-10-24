"""
app/backtesting — Standalone Historical Backtesting Module for Options Desk.
Completely isolated from live analysis, the scheduler, paper-trading DB records, and execution routes.
"""

from app.backtesting.data_validator import (
    HistoricalDataValidator,
    DataQualityReport,
    HISTORICAL_SCHEMA_COLUMNS,
)
from app.backtesting.fno_downloader import FnoHistoricalDownloader
from app.backtesting.backtest_engine import (
    HistoricalBacktestEngine,
    BacktestTrade,
    StrategyMetrics,
)

__all__ = [
    "HistoricalDataValidator",
    "DataQualityReport",
    "HISTORICAL_SCHEMA_COLUMNS",
    "FnoHistoricalDownloader",
    "HistoricalBacktestEngine",
    "BacktestTrade",
    "StrategyMetrics",
]
