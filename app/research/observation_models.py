"""
app/research/observation_models.py — Persistent Schema for Iron Condor Paper-Trading Observation Mode

Stores isolated audit records for:
1. ObservationSignalLog: Evaluated signals, candidate specs, regime, and rejection rationales.
2. ObservationPosition: Executed Iron Condor positions, timing, spot levels, credits, debits, P&L.
3. ObservationLeg: Exact 4 legs with fill prices, top-of-book bid/ask quotes, source, and timestamps.
4. ObservationQuoteSnapshot: Quote freshness, bid/ask spread %, uncrossed verification, and quote source.
5. ObservationDataQualityEvent: Data-quality rejections, missing/stale/crossed quotes, valuation unavailable flags.
6. ObservationRiskAudit: Risk checks, maximum loss limits, circuit breaker evaluations.
"""

import sqlite3
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

IST = ZoneInfo("Asia/Kolkata")
OBSERVATION_DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "instance", "research_paper_trading")
OBSERVATION_DB_PATH = os.path.join(OBSERVATION_DB_DIR, "observation_ledger.db")


def get_db_connection(db_path: str = OBSERVATION_DB_PATH) -> sqlite3.Connection:
    """Returns an isolated SQLite connection with WAL mode enabled."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_observation_db(db_path: str = OBSERVATION_DB_PATH) -> None:
    """Initializes tables and indexes for the research observation ledger."""
    with get_db_connection(db_path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS observation_signals (
            signal_id TEXT PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            strategy_id TEXT NOT NULL,
            engine_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            expiry TEXT NOT NULL,
            spot_price REAL NOT NULL,
            market_regime TEXT NOT NULL,
            market_bias TEXT NOT NULL,
            confidence_score REAL NOT NULL,
            status TEXT NOT NULL,            -- 'ELIGIBLE' or 'REJECTED'
            rejection_reason TEXT,
            short_put REAL,
            long_put REAL,
            short_call REAL,
            long_call REAL,
            net_credit_pts REAL,
            wing_width REAL,
            max_profit_inr REAL,
            max_loss_inr REAL,
            strategy_version TEXT NOT NULL,
            data_source TEXT NOT NULL,
            fingerprint TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS observation_positions (
            position_id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            engine_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            expiry TEXT NOT NULL,
            lots INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            entry_time TIMESTAMP NOT NULL,
            entry_spot REAL NOT NULL,
            entry_net_credit_pts REAL NOT NULL,
            target_net_debit_pts REAL NOT NULL,  -- <= 30% of entry credit
            stop_loss_debit_pts REAL NOT NULL,   -- >= 150% of entry credit
            max_profit_inr REAL NOT NULL,
            max_loss_inr REAL NOT NULL,
            
            -- Exit information
            exit_time TIMESTAMP,
            exit_spot REAL,
            exit_net_debit_pts REAL,
            exit_reason TEXT,                   -- 'TARGET_HIT', 'STOP_LOSS', 'SESSION_CUTOFF', 'MANUAL_CLOSE'
            
            -- P&L & Costs Breakdown (Base Scenario)
            gross_pnl_inr REAL,
            net_pnl_inr REAL,
            total_costs_inr REAL,
            brokerage_inr REAL,
            stt_inr REAL,
            exchange_fees_inr REAL,
            gst_inr REAL,
            stamp_duty_inr REAL,
            sebi_ipft_inr REAL,

            -- Conservative Execution Scenario Comparison (10 bps slippage per side)
            conservative_exit_net_debit_pts REAL,
            conservative_gross_pnl_inr REAL,
            conservative_total_costs_inr REAL,
            conservative_net_pnl_inr REAL,
            conservative_slippage_cost_inr REAL,
            
            status TEXT NOT NULL,               -- 'OPEN', 'CLOSED', 'VALUATION_UNAVAILABLE'
            strategy_version TEXT NOT NULL,
            data_source TEXT NOT NULL,
            FOREIGN KEY (signal_id) REFERENCES observation_signals(signal_id)
        );

        CREATE TABLE IF NOT EXISTS observation_legs (
            leg_id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            leg_index INTEGER NOT NULL,         -- 0: Put Short, 1: Put Long, 2: Call Short, 3: Call Long
            symbol TEXT NOT NULL,
            strike REAL NOT NULL,
            option_type TEXT NOT NULL,          -- 'PE' or 'CE'
            action TEXT NOT NULL,               -- 'SELL' or 'BUY'
            quantity INTEGER NOT NULL,
            
            -- Entry execution and counterpart quotes
            entry_fill_price REAL NOT NULL,
            entry_bid REAL NOT NULL,
            entry_ask REAL NOT NULL,
            entry_spread_pct REAL NOT NULL,
            entry_quote_timestamp TIMESTAMP NOT NULL,
            entry_quote_source TEXT NOT NULL,
            
            -- Exit execution and counterpart quotes
            exit_fill_price REAL,
            exit_bid REAL,
            exit_ask REAL,
            exit_spread_pct REAL,
            exit_quote_timestamp TIMESTAMP,
            exit_quote_source TEXT,
            
            FOREIGN KEY (position_id) REFERENCES observation_positions(position_id)
        );

        CREATE TABLE IF NOT EXISTS observation_data_quality_events (
            event_id TEXT PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            event_type TEXT NOT NULL,           -- 'STALE_QUOTE', 'CROSSED_MARKET', 'MISSING_QUOTE', 'EXCESSIVE_SPREAD', 'VALUATION_UNAVAILABLE'
            symbol TEXT NOT NULL,
            strike REAL,
            option_type TEXT,
            details TEXT NOT NULL,
            quote_source TEXT NOT NULL,
            position_id TEXT,
            FOREIGN KEY (position_id) REFERENCES observation_positions(position_id)
        );

        CREATE TABLE IF NOT EXISTS observation_risk_audits (
            audit_id TEXT PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            action TEXT NOT NULL,               -- 'ENTRY_EVALUATION', 'CIRCUIT_BREAKER_CHECK', 'COOLDOWN_ACTIVE'
            approved INTEGER NOT NULL,          -- 1 or 0
            reason TEXT NOT NULL,
            max_loss_inr REAL,
            daily_loss_inr REAL,
            open_risk_inr REAL
        );

        CREATE INDEX IF NOT EXISTS idx_obs_signals_created ON observation_signals(created_at);
        CREATE INDEX IF NOT EXISTS idx_obs_positions_status ON observation_positions(status);
        CREATE INDEX IF NOT EXISTS idx_obs_legs_pos_id ON observation_legs(position_id);
        CREATE INDEX IF NOT EXISTS idx_obs_dq_created ON observation_data_quality_events(created_at);
        """)
