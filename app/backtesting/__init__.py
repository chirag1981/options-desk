"""
app/backtesting — Standalone Historical Backtesting Module for Options Desk.
Completely isolated from live analysis, the scheduler, paper-trading DB records, and execution routes.
"""

from app.backtesting.data_validator import (
    HistoricalDataValidator,
    DataQualityReport,
    HISTORICAL_SCHEMA_COLUMNS,
)

__all__ = [
    "HistoricalDataValidator",
    "DataQualityReport",
    "HISTORICAL_SCHEMA_COLUMNS",
]
