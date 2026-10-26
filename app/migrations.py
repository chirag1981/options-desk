"""
app/migrations.py — Backward-Compatible Schema Migration Helper for Production/Paper SQLite DB

Applies non-destructive ALTER TABLE additions to SQLite tables if columns are missing.
Ensures zero data loss and flawless backward compatibility with existing databases.
"""

import sqlite3
import logging
from typing import List, Dict, Tuple

log = logging.getLogger("migrations")


def run_database_migrations(connection: sqlite3.Connection) -> None:
    """
    Applies additive column migrations and creates new tables if not present.
    Safe to run repeatedly on startup or test fixtures.
    """
    cursor = connection.cursor()

    # 1. Existing table: trade_positions - Add itemized cost columns
    cursor.execute("PRAGMA table_info(trade_positions);")
    existing_tp_cols = {row[1] for row in cursor.fetchall()}

    new_tp_cols = [
        ("entry_brokerage_inr", "REAL DEFAULT 0.0"),
        ("entry_stt_inr", "REAL DEFAULT 0.0"),
        ("entry_exchange_fees_inr", "REAL DEFAULT 0.0"),
        ("entry_gst_inr", "REAL DEFAULT 0.0"),
        ("entry_stamp_duty_inr", "REAL DEFAULT 0.0"),
        ("entry_sebi_ipft_inr", "REAL DEFAULT 0.0"),
        ("exit_brokerage_inr", "REAL DEFAULT 0.0"),
        ("exit_stt_inr", "REAL DEFAULT 0.0"),
        ("exit_exchange_fees_inr", "REAL DEFAULT 0.0"),
        ("exit_gst_inr", "REAL DEFAULT 0.0"),
        ("exit_stamp_duty_inr", "REAL DEFAULT 0.0"),
        ("exit_sebi_ipft_inr", "REAL DEFAULT 0.0"),
    ]

    for col_name, col_type in new_tp_cols:
        if col_name not in existing_tp_cols:
            cursor.execute(f"ALTER TABLE trade_positions ADD COLUMN {col_name} {col_type};")
            log.info(f"Migrated trade_positions: added column {col_name}")

    # 2. Existing table: position_legs - Add quote evidence and timestamp columns
    cursor.execute("PRAGMA table_info(position_legs);")
    existing_pl_cols = {row[1] for row in cursor.fetchall()}

    new_pl_cols = [
        ("entry_bid", "REAL"),
        ("entry_ask", "REAL"),
        ("entry_spread_pct", "REAL"),
        ("entry_quote_timestamp", "TIMESTAMP"),
        ("entry_data_source", "TEXT DEFAULT 'FYERS_LIVE'"),
        ("exit_bid", "REAL"),
        ("exit_ask", "REAL"),
        ("exit_spread_pct", "REAL"),
        ("exit_quote_timestamp", "TIMESTAMP"),
        ("exit_data_source", "TEXT"),
    ]

    for col_name, col_type in new_pl_cols:
        if col_name not in existing_pl_cols:
            cursor.execute(f"ALTER TABLE position_legs ADD COLUMN {col_name} {col_type};")
            log.info(f"Migrated position_legs: added column {col_name}")

    # 3. Create table: data_quality_events if not exists
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS data_quality_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP NOT NULL,
        candidate_id TEXT,
        event_type TEXT NOT NULL,
        symbol TEXT,
        strike REAL,
        option_type TEXT,
        details TEXT NOT NULL,
        data_source TEXT DEFAULT 'FYERS_LIVE',
        idempotency_key TEXT UNIQUE
    );
    """)

    # 4. Create table: position_mtm_snapshots if not exists
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS position_mtm_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id INTEGER NOT NULL,
        timestamp TIMESTAMP NOT NULL,
        spot_price REAL NOT NULL,
        net_debit_pts REAL,
        unrealized_gross_pnl_inr REAL,
        known_transaction_costs_inr REAL DEFAULT 0.0,
        unrealized_net_pnl_inr REAL,
        data_quality_status TEXT DEFAULT 'OK',
        leg_prices_json TEXT,
        idempotency_key TEXT UNIQUE,
        FOREIGN KEY (position_id) REFERENCES trade_positions(id) ON DELETE CASCADE
    );
    """)

    # Indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dqe_timestamp ON data_quality_events(timestamp);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dqe_candidate ON data_quality_events(candidate_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mtm_pos_id ON position_mtm_snapshots(position_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mtm_timestamp ON position_mtm_snapshots(timestamp);")

    connection.commit()
