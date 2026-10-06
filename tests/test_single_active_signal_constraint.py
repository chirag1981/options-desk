"""
tests/test_single_active_signal_constraint.py - Unit Tests for Single Active Position Invariant per Symbol

Tests:
1. Concurrent record_signal calls for the same symbol create exactly one row.
2. Paper and non-paper trades are both blocked if an active position exists.
3. Opposite side (PE while CE is open) is blocked while open.
4. Different symbols (NIFTY, BANKNIFTY, FINNIFTY) are allowed in parallel.
5. New signal is allowed after position is closed and 15m cooldown has elapsed.
6. DB migration cleans up pre-existing duplicate open signals, closing them with exit_reason='DUPLICATE_CLEANUP'.
"""

import os
import json
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.services.options_signal_service import (
    _get_db,
    init_signal_db,
    record_signal,
    get_signals_summary,
    close_signal_manually,
)

IST = ZoneInfo("Asia/Kolkata")


class TestSingleActiveSignalConstraint(unittest.TestCase):

    def setUp(self):
        init_signal_db()
        # Clear signals table for isolated test runs
        with _get_db() as conn:
            conn.execute("DELETE FROM signals")

        self.sample_signal_ce = {
            "symbol": "NIFTY",
            "type": "CE",
            "contract_name": "24000 CE",
            "strike": 24000.0,
            "expiry": "08-Oct-2026",
            "spot_price": 24020.0,
            "entry_price": 150.0,
            "ask_at_entry": 150.0,
            "bid_at_entry": 148.5,
            "stop_loss": 120.0,
            "target_1": 195.0,
            "target_2": 225.0,
            "risk_reward": "1:2.0",
            "setup_score": 85,
            "reason": "Bullish Breakout",
            "pcr": 1.25,
            "bias": "BULLISH",
            "atm_iv": 14.5,
            "pillar_flags": {"direction": True, "level": True, "momentum": True, "volume": True, "oi": True, "iv_session": True},
            "raw_values": {},
            "data_quality": {"valid": True},
        }

        self.sample_signal_pe = {
            "symbol": "NIFTY",
            "type": "PE",
            "contract_name": "24000 PE",
            "strike": 24000.0,
            "expiry": "08-Oct-2026",
            "spot_price": 23980.0,
            "entry_price": 140.0,
            "ask_at_entry": 140.0,
            "bid_at_entry": 138.5,
            "stop_loss": 110.0,
            "target_1": 185.0,
            "target_2": 215.0,
            "risk_reward": "1:2.0",
            "setup_score": 85,
            "reason": "Bearish Breakdown",
            "pcr": 0.75,
            "bias": "BEARISH",
            "atm_iv": 15.0,
            "pillar_flags": {"direction": True, "level": True, "momentum": True, "volume": True, "oi": True, "iv_session": True},
            "raw_values": {},
            "data_quality": {"valid": True},
        }

    def test_concurrent_record_signal_same_symbol(self):
        """Test 1: Multiple concurrent record_signal threads for the same symbol create exactly ONE row."""
        results = []
        threads = []

        def worker():
            res = record_signal(dict(self.sample_signal_ce), is_paper_trade=False)
            if res:
                results.append(res)

        for _ in range(5):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(results), 1)

        with _get_db() as conn:
            open_rows = conn.execute("SELECT * FROM signals WHERE symbol = 'NIFTY' AND status = 'ACTIVE'").fetchall()
            self.assertEqual(len(open_rows), 1)

    def test_paper_and_non_paper_both_blocked_when_active(self):
        """Test 2: When an active position exists, subsequent paper and live signals are both blocked."""
        # Insert initial live position
        first = record_signal(dict(self.sample_signal_ce), is_paper_trade=False)
        self.assertIsNotNone(first)

        # Attempt to insert paper trade for same symbol -> MUST BE BLOCKED
        paper_attempt = record_signal(dict(self.sample_signal_ce), is_paper_trade=True)
        self.assertIsNone(paper_attempt)

        # Attempt to insert live trade for same symbol -> MUST BE BLOCKED
        live_attempt = record_signal(dict(self.sample_signal_ce), is_paper_trade=False)
        self.assertIsNone(live_attempt)

    def test_opposite_side_blocked_while_open(self):
        """Test 3: Opposite side signal (PE when CE is active) is blocked while position is open."""
        ce_trade = record_signal(dict(self.sample_signal_ce), is_paper_trade=False)
        self.assertIsNotNone(ce_trade)

        # Try to open PE on NIFTY while CE is open -> MUST BE BLOCKED
        pe_attempt = record_signal(dict(self.sample_signal_pe), is_paper_trade=False)
        self.assertIsNone(pe_attempt)

    def test_different_symbols_allowed_in_parallel(self):
        """Test 4: Different index symbols (NIFTY, BANKNIFTY, SENSEX) can have 1 active position each."""
        nifty_trade = record_signal(dict(self.sample_signal_ce, symbol="NIFTY"))
        banknifty_trade = record_signal(dict(self.sample_signal_ce, symbol="BANKNIFTY", contract_name="54500 CE", strike=54500))
        sensex_trade = record_signal(dict(self.sample_signal_ce, symbol="SENSEX", contract_name="72000 CE", strike=72000))

        self.assertIsNotNone(nifty_trade)
        self.assertIsNotNone(banknifty_trade)
        self.assertIsNotNone(sensex_trade)

        summary = get_signals_summary()
        self.assertEqual(summary["metrics"]["active_count"], 3)

    def test_new_signal_allowed_after_close_and_cooldown(self):
        """Test 5: New signal is allowed after the trade is closed and cooldown (15m) has passed."""
        trade = record_signal(dict(self.sample_signal_ce), is_paper_trade=False)
        self.assertIsNotNone(trade)

        # Close trade manually
        closed = close_signal_manually(trade["id"])
        self.assertEqual(closed["status"], "CLOSED")

        # Set exit_time to 20 minutes ago to simulate cooldown expiry
        past_time = (datetime.now(IST) - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
        with _get_db() as conn:
            conn.execute("UPDATE signals SET exit_time = ?, updated_at = ? WHERE id = ?", (past_time, past_time, trade["id"]))

        # Now new signal should succeed
        new_trade = record_signal(dict(self.sample_signal_pe), is_paper_trade=False)
        self.assertIsNotNone(new_trade)

    def test_migration_cleans_up_preexisting_duplicates(self):
        """Test 6: Database migration detects duplicate active rows, keeps oldest, and closes others with DUPLICATE_CLEANUP."""
        # Temporarily drop partial unique index to simulate legacy database state
        with _get_db() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_signals_unique_active_symbol")
            now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            # Insert 3 active NIFTY trades
            conn.execute("""
                INSERT INTO signals (symbol, signal_type, contract_name, strike, spot_at_entry, entry_price, stop_loss, target_1, target_2, status, current_price, highest_price, lowest_price, created_at, updated_at)
                VALUES ('NIFTY', 'CE', '24000 CE', 24000, 24000, 100.0, 80.0, 130.0, 150.0, 'ACTIVE', 100.0, 100.0, 100.0, ?, ?)
            """, (now_str, now_str))
            conn.execute("""
                INSERT INTO signals (symbol, signal_type, contract_name, strike, spot_at_entry, entry_price, stop_loss, target_1, target_2, status, current_price, highest_price, lowest_price, created_at, updated_at)
                VALUES ('NIFTY', 'CE', '24100 CE', 24100, 24050, 80.0, 60.0, 110.0, 130.0, 'ACTIVE', 80.0, 80.0, 80.0, ?, ?)
            """, (now_str, now_str))
            conn.execute("""
                INSERT INTO signals (symbol, signal_type, contract_name, strike, spot_at_entry, entry_price, stop_loss, target_1, target_2, status, current_price, highest_price, lowest_price, created_at, updated_at)
                VALUES ('NIFTY', 'PE', '23900 PE', 23900, 23950, 90.0, 70.0, 120.0, 140.0, 'ACTIVE', 90.0, 90.0, 90.0, ?, ?)
            """, (now_str, now_str))

        # Run migration
        init_signal_db()

        with _get_db() as conn:
            open_nifty = conn.execute("SELECT * FROM signals WHERE symbol = 'NIFTY' AND status = 'ACTIVE'").fetchall()
            cleaned_nifty = conn.execute("SELECT * FROM signals WHERE symbol = 'NIFTY' AND exit_reason = 'DUPLICATE_CLEANUP'").fetchall()

            # Exactly 1 active trade remaining (the oldest)
            self.assertEqual(len(open_nifty), 1)
            # 2 duplicate trades closed with DUPLICATE_CLEANUP
            self.assertEqual(len(cleaned_nifty), 2)


if __name__ == "__main__":
    unittest.main()
