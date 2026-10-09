"""
tests/test_actionable_preventions.py — Unit Tests for Actionable Trade Failure Preventions

Tests the 3 key preventions identified by the AI Trade Review Diagnostic:
1. Consecutive Loss Cooldown: Blocks new signals for 20 minutes after 2 consecutive SL_HIT trades.
2. Major Round Strike Defense: Blocks BUY CE right under 500/1000 major round resistance with active Call writing; blocks BUY PE right above support with active Put writing.
3. Early Breakeven Trailing Stop: Immunizes trade against RIGHT_THEN_REVERSED by trailing SL to Entry + 0.50 at +4% / +0.4R / +5 pts profit.
"""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.services.options_engine import analyze_option_desk, ENGINE_CONFIG
from app.services.options_signal_service import (
    _get_db,
    init_signal_db,
    record_signal,
    update_active_signals,
    check_consecutive_loss_cooldown,
)

IST = ZoneInfo("Asia/Kolkata")


class TestActionablePreventions(unittest.TestCase):

    def setUp(self):
        init_signal_db()
        with _get_db() as conn:
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM trade_ticks")
            conn.execute("DELETE FROM trade_reviews")

    def tearDown(self):
        with _get_db() as conn:
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM trade_ticks")
            conn.execute("DELETE FROM trade_reviews")

    def _sample_chain_data(self, spot=22495.0, entry_price=100.0, is_ce=True, strike=22500):
        """Builds a test options chain payload."""
        ce_ask = entry_price if is_ce else 40.0
        ce_bid = ce_ask - 0.5
        pe_ask = 40.0 if is_ce else entry_price
        pe_bid = pe_ask - 0.5

        chain = [
            {
                "strike": strike,
                "ce_ltp": ce_ask,
                "ce_bid": ce_bid,
                "ce_ask": ce_ask,
                "pe_ltp": pe_ask,
                "pe_bid": pe_bid,
                "pe_ask": pe_ask,
                "ce_oi": 150000,
                "pe_oi": 250000 if is_ce else 50000,
                "ce_change_oi": 50000 if is_ce else 60000,  # Active Call writing at 22500
                "pe_change_oi": 80000 if is_ce else -20000,
                "ce_volume": 500000 if is_ce else 100000,
                "pe_volume": 100000 if is_ce else 500000,
                "ce_iv": 15.0,
                "pe_iv": 15.0,
                "ce_delta": 0.50,
                "pe_delta": -0.50,
            }
        ]
        return {
            "symbol": "NIFTY",
            "spot_price": spot,
            "spot_change_pct": 0.40 if is_ce else -0.40,
            "strike_step": 50,
            "data_status": "LIVE",
            "chain": chain,
            "prev_close": 22400.0 if is_ce else 22600.0,
            "open_price": 22420.0 if is_ce else 22580.0,
            "prev_high": 22480.0 if is_ce else 22620.0,
            "prev_low": 22380.0 if is_ce else 22520.0,
            "price_history": [22400.0, 22480.0, 22450.0, spot],
        }

    # -------------------------------------------------------------------------
    # Prevention 1: Consecutive Loss Cooldown
    # -------------------------------------------------------------------------
    def test_consecutive_loss_cooldown_triggers_after_2_sl_hits(self):
        """2 consecutive SL_HIT trades within 20m activate cooldown and block new signals."""
        now_time = datetime.now(IST)
        exit_time_1 = (now_time - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        exit_time_2 = (now_time - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

        with _get_db() as conn:
            # 1st SL_HIT
            conn.execute("""
                INSERT INTO signals (id, symbol, signal_type, contract_name, strike, expiry,
                                    spot_at_entry, entry_price, stop_loss, target_1, target_2,
                                    current_price, highest_price, lowest_price,
                                    status, exit_reason, exit_time, is_deleted, is_paper_trade, created_at, updated_at)
                VALUES (101, 'NIFTY', 'CE', '22500 CE', 22500, '15-Oct-2026', 22480, 100, 92, 112, 120, 92, 100, 92, 'SL_HIT', 'SL_HIT', ?, 0, 1, ?, ?)
            """, (exit_time_1, exit_time_1, exit_time_1))
            # 2nd SL_HIT
            conn.execute("""
                INSERT INTO signals (id, symbol, signal_type, contract_name, strike, expiry,
                                    spot_at_entry, entry_price, stop_loss, target_1, target_2,
                                    current_price, highest_price, lowest_price,
                                    status, exit_reason, exit_time, is_deleted, is_paper_trade, created_at, updated_at)
                VALUES (102, 'NIFTY', 'CE', '22500 CE', 22500, '15-Oct-2026', 22485, 100, 92, 112, 120, 92, 100, 92, 'SL_HIT', 'SL_HIT', ?, 0, 1, ?, ?)
            """, (exit_time_2, exit_time_2, exit_time_2))

        # Verify check_consecutive_loss_cooldown returns True and remaining minutes ~ 15
        is_cooldown, reason_msg, rem_sec = check_consecutive_loss_cooldown(symbol="NIFTY", now_dt=now_time)
        self.assertTrue(is_cooldown)
        self.assertIn("CONSECUTIVE_LOSS_COOLDOWN", reason_msg)
        self.assertAlmostEqual(rem_sec / 60.0, 15.0, delta=1.0)

        # Verify record_signal blocks creating a new signal during cooldown
        future_exp = (datetime.now(IST) + timedelta(days=7)).strftime("%d-%b-%Y")
        sig_data = {
            "symbol": "NIFTY",
            "type": "CE",
            "contract_name": "22500 CE",
            "strike": 22500.0,
            "expiry": future_exp,
            "spot_price": 22480.0,
            "entry_price": 100.0,
            "stop_loss": 92.0,
            "target_1": 112.0,
            "target_2": 120.0,
            "lots": 1,
            "lot_size": 65,
        }
        res = record_signal(sig_data, is_paper_trade=True)
        self.assertIsNone(res, "record_signal should return None when in consecutive loss cooldown")

    def test_consecutive_loss_cooldown_expires_after_20_minutes(self):
        """Cooldown clears after 20 minutes have elapsed since the last SL_HIT."""
        now_time = datetime.now(IST)
        exit_time_1 = (now_time - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        exit_time_2 = (now_time - timedelta(minutes=21)).strftime("%Y-%m-%d %H:%M:%S")

        with _get_db() as conn:
            conn.execute("""
                INSERT INTO signals (id, symbol, signal_type, contract_name, strike, expiry,
                                    spot_at_entry, entry_price, stop_loss, target_1, target_2,
                                    current_price, highest_price, lowest_price,
                                    status, exit_reason, exit_time, is_deleted, is_paper_trade, created_at, updated_at)
                VALUES (201, 'NIFTY', 'CE', '22500 CE', 22500, '15-Oct-2026', 22480, 100, 92, 112, 120, 92, 100, 92, 'SL_HIT', 'SL_HIT', ?, 0, 1, ?, ?)
            """, (exit_time_1, exit_time_1, exit_time_1))
            conn.execute("""
                INSERT INTO signals (id, symbol, signal_type, contract_name, strike, expiry,
                                    spot_at_entry, entry_price, stop_loss, target_1, target_2,
                                    current_price, highest_price, lowest_price,
                                    status, exit_reason, exit_time, is_deleted, is_paper_trade, created_at, updated_at)
                VALUES (202, 'NIFTY', 'CE', '22500 CE', 22500, '15-Oct-2026', 22485, 100, 92, 112, 120, 92, 100, 92, 'SL_HIT', 'SL_HIT', ?, 0, 1, ?, ?)
            """, (exit_time_2, exit_time_2, exit_time_2))

        is_cooldown, reason_msg, rem_sec = check_consecutive_loss_cooldown(symbol="NIFTY", now_dt=now_time)
        self.assertFalse(is_cooldown)
        self.assertEqual(reason_msg, "")

    # -------------------------------------------------------------------------
    # Prevention 2: Major Round Strike Defense Gate
    # -------------------------------------------------------------------------
    def test_major_round_resistance_blocks_ce_near_500_multiple(self):
        """Blocks BUY CE when Spot is within 20 pts under 22500 with Call OI addition."""
        # Spot at 22495 (within 20 pts of 22500 round strike)
        data = self._sample_chain_data(spot=22495.0, entry_price=100.0, is_ce=True, strike=22500)
        res = analyze_option_desk(data, has_active_trade=False)
        
        opt_buying = res["option_buying"]
        self.assertEqual(opt_buying["decision"], "WAIT")
        self.assertIn("MAJOR_ROUND_RESISTANCE_BLOCK", opt_buying["reason"])
        self.assertIn("22500", opt_buying["reason"])

    # -------------------------------------------------------------------------
    # Prevention 3: Early Breakeven Trailing Stop
    # -------------------------------------------------------------------------
    def test_early_breakeven_trailing_stop_moves_sl_to_cost_plus_buffer(self):
        """+5.5 pts profit moves SL from 92.0 to Entry + 0.50 (100.50), protecting against reversal."""
        future_exp = (datetime.now(IST) + timedelta(days=7)).strftime("%d-%b-%Y")
        sig_data = {
            "symbol": "NIFTY",
            "type": "CE",
            "contract_name": "22500 CE",
            "strike": 22500.0,
            "expiry": future_exp,
            "spot_price": 22480.0,
            "entry_price": 100.0,
            "ask_at_entry": 100.0,
            "bid_at_entry": 99.5,
            "stop_loss": 92.0,
            "target_1": 112.0,
            "target_2": 120.0,
            "lots": 1,
            "lot_size": 65,
        }
        sig = record_signal(sig_data, is_paper_trade=False)
        self.assertIsNotNone(sig)

        # Price ticks up to 105.50 (+5.5 pts MFE, +5.5% profit)
        market_cache_rally = {
            "NIFTY": {
                "spot_price": 22510.0,
                "chain": [{"strike": 22500, "ce_ltp": 105.5, "ce_bid": 105.0, "ce_ask": 106.0}],
            }
        }
        now_time = datetime.now(IST)
        updated = update_active_signals(market_data_cache=market_cache_rally, now_dt=now_time)
        
        # Verify SL is trailed to at least Entry + 0.50 = 100.50
        self.assertEqual(len(updated), 1)
        self.assertGreaterEqual(updated[0]["stop_loss"], 100.50)
        self.assertEqual(updated[0]["status"], "ACTIVE")

        # Next tick: Price reverses to 100.20 (below trailed SL 100.50)
        market_cache_drop = {
            "NIFTY": {
                "spot_price": 22475.0,
                "chain": [{"strike": 22500, "ce_ltp": 100.20, "ce_bid": 100.0, "ce_ask": 100.5}],
            }
        }
        updated_exit = update_active_signals(market_data_cache=market_cache_drop, now_dt=now_time + timedelta(seconds=10))
        
        # Verify trade exits with TSL_HIT and preserves capital / small gain (no loss)
        self.assertEqual(updated_exit[0]["status"], "TSL_HIT")
        self.assertEqual(updated_exit[0]["exit_reason"], "TRAILING_STOP_HIT")
        self.assertGreaterEqual(updated_exit[0]["points_pnl"], 0.0)
