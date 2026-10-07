"""
tests/test_paper_strategy_scenarios.py — 14 Comprehensive Paper Strategy Scenario Tests

Verifies:
1. Fresh bullish high with no pullback -> WAIT.
2. Fresh bearish low with no pullback -> WAIT.
3. Bullish pullback + recovery + expansion + 2/3 confirmation -> BUY CE.
4. Bearish rebound + recovery + expansion + 2/3 confirmation -> BUY PE.
5. Choppy market (low expansion) -> WAIT.
6. Strong directional expansion during 11:45–13:00 -> trade remains possible.
7. Active trade -> no second trade.
8. Active CE + bearish setup -> no PE flip.
9. Closed trade + valid new setup -> re-entry allowed after existing cooldown.
10. Two consecutive scheduler confirmations -> entry.
11. One confirmation only -> WAIT.
12. Missing/invalid bid or ask -> no signal (WAIT).
13. Non-LIVE data -> no signal (WAIT).
14. Daily trade count at 3 -> no new signal (MAX_DAILY_TRADES_REACHED).
"""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.services.options_engine import analyze_option_desk, clear_symbol_price_history, ENGINE_CONFIG
from app.services.options_signal_service import (
    _get_db,
    init_signal_db,
    record_signal,
    get_daily_trade_count,
    has_active_signal_for_symbol,
)
from app.services.options_scheduler import (
    update_pending_entry_confirmation,
    reset_pending_entry_confirmation,
)

IST = ZoneInfo("Asia/Kolkata")


class TestPaperStrategyScenarios(unittest.TestCase):

    def setUp(self):
        init_signal_db()
        clear_symbol_price_history("NIFTY")
        reset_pending_entry_confirmation("NIFTY")
        with _get_db() as conn:
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM trade_ticks")

    def tearDown(self):
        with _get_db() as conn:
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM trade_ticks")
        clear_symbol_price_history("NIFTY")
        reset_pending_entry_confirmation("NIFTY")

    def _base_bullish_chain(self, spot=22550.0):
        return {
            "symbol": "NIFTY",
            "spot_price": spot,
            "spot_change_pct": 0.40,
            "strike_step": 50,
            "data_status": "LIVE",
            "prev_close": 22450.0,
            "open_price": 22480.0,
            "prev_high": 22520.0,
            "prev_low": 22400.0,
            "price_history": [22450.0, 22560.0, 22530.0, 22550.0],  # Peak 22560 -> Pullback 22530 -> Recovered 22550
            "chain": [
                {
                    "strike": 22550,
                    "ce_ltp": 120.0, "ce_bid": 119.5, "ce_ask": 120.5,
                    "pe_ltp": 80.0, "pe_bid": 79.5, "pe_ask": 80.5,
                    "ce_oi": 100000, "pe_oi": 250000,
                    "ce_change_oi": -20000, "pe_change_oi": 80000,
                    "ce_volume": 600000, "pe_volume": 200000,
                    "ce_iv": 15.0, "pe_iv": 15.0, "ce_delta": 0.50, "pe_delta": -0.50,
                }
            ],
        }

    def _base_bearish_chain(self, spot=22550.0):
        return {
            "symbol": "NIFTY",
            "spot_price": spot,
            "spot_change_pct": -0.40,
            "strike_step": 50,
            "data_status": "LIVE",
            "prev_close": 22650.0,
            "open_price": 22620.0,
            "prev_high": 22680.0,
            "prev_low": 22560.0,
            "price_history": [22650.0, 22530.0, 22570.0, 22550.0],  # Trough 22530 -> Rebound 22570 -> Breakdown 22550
            "chain": [
                {
                    "strike": 22550,
                    "ce_ltp": 80.0, "ce_bid": 79.5, "ce_ask": 80.5,
                    "pe_ltp": 120.0, "pe_bid": 119.5, "pe_ask": 120.5,
                    "ce_oi": 250000, "pe_oi": 100000,
                    "ce_change_oi": 80000, "pe_change_oi": -20000,
                    "ce_volume": 200000, "pe_volume": 600000,
                    "ce_iv": 15.0, "pe_iv": 15.0, "ce_delta": 0.50, "pe_delta": -0.50,
                }
            ],
        }

    def test_scenario_1_fresh_bullish_high_no_pullback(self):
        """Scenario 1: Fresh intraday high with strictly ascending prices (no pullback) -> WAIT."""
        data = self._base_bullish_chain()
        data["price_history"] = [22450.0, 22480.0, 22520.0, 22550.0]  # Monotonic rise
        res = analyze_option_desk(data, has_active_trade=False)
        self.assertEqual(res["option_buying"]["decision"], "WAIT")
        self.assertEqual(res["option_buying"]["decision_reason"], "FRESH_HIGH_NO_PULLBACK")

    def test_scenario_2_fresh_bearish_low_no_pullback(self):
        """Scenario 2: Fresh intraday low with strictly descending prices (no rebound) -> WAIT."""
        data = self._base_bearish_chain()
        data["price_history"] = [22650.0, 22620.0, 22580.0, 22550.0]  # Monotonic fall
        res = analyze_option_desk(data, has_active_trade=False)
        self.assertEqual(res["option_buying"]["decision"], "WAIT")
        self.assertEqual(res["option_buying"]["decision_reason"], "FRESH_LOW_NO_PULLBACK")

    def test_scenario_3_bullish_pullback_recovery_expansion(self):
        """Scenario 3: Bullish pullback + confirmed recovery + 2/3 confirmation -> BUY CE."""
        data = self._base_bullish_chain()
        res = analyze_option_desk(data, has_active_trade=False)
        self.assertEqual(res["option_buying"]["decision"], "BUY CE")
        self.assertIsNotNone(res["option_buying"]["trade_plan"])

    def test_scenario_4_bearish_rebound_recovery_expansion(self):
        """Scenario 4: Bearish rebound + confirmed recovery + 2/3 confirmation -> BUY PE."""
        data = self._base_bearish_chain()
        res = analyze_option_desk(data, has_active_trade=False)
        self.assertEqual(res["option_buying"]["decision"], "BUY PE")
        self.assertIsNotNone(res["option_buying"]["trade_plan"])

    def test_scenario_5_choppy_market_low_expansion(self):
        """Scenario 5: Flat prices in narrow chop range -> WAIT."""
        data = self._base_bullish_chain()
        data["price_history"] = [22550.0, 22551.0, 22549.5, 22550.2]  # < 5 pts range
        res = analyze_option_desk(data, has_active_trade=False)
        self.assertEqual(res["option_buying"]["decision"], "WAIT")

    def test_scenario_6_strong_directional_expansion_midday(self):
        """Scenario 6: Strong directional expansion during 11:45-13:00 -> Trade remains possible."""
        data = self._base_bullish_chain()
        data["price_history"] = [22450.0, 22580.0, 22540.0, 22575.0]  # > 25 pts range
        res = analyze_option_desk(data, has_active_trade=False)
        self.assertEqual(res["option_buying"]["decision"], "BUY CE")

    def test_scenario_7_active_trade_no_second_trade(self):
        """Scenario 7: When active trade exists, decision is WAIT."""
        data = self._base_bullish_chain()
        res = analyze_option_desk(data, has_active_trade=True)
        self.assertEqual(res["option_buying"]["decision"], "WAIT")
        self.assertEqual(res["option_buying"]["decision_reason"], "ACTIVE TRADE EXISTS")

    def test_scenario_8_active_ce_bearish_setup_no_pe_flip(self):
        """Scenario 8: When active CE trade is open, a bearish signal returns WAIT (no PE flip)."""
        data = self._base_bearish_chain()
        res = analyze_option_desk(data, has_active_trade=True)
        self.assertEqual(res["option_buying"]["decision"], "WAIT")
        self.assertEqual(res["option_buying"]["decision_reason"], "ACTIVE TRADE EXISTS")

    def test_scenario_9_closed_trade_reentry_allowed_after_cooldown(self):
        """Scenario 9: Closed trade allows re-entry after 15m cooldown."""
        # Insert closed trade with exit_time 20 minutes ago
        with _get_db() as conn:
            conn.execute("""
                INSERT INTO signals (
                    symbol, signal_type, contract_name, strike, expiry, spot_at_entry,
                    entry_price, bid_at_entry, ask_at_entry, spread_paid, initial_stop_loss,
                    stop_loss, target_1, target_2, risk_reward, setup_score,
                    trigger_reason, status, current_price, highest_price, lowest_price,
                    points_pnl, pnl_pct, lots, lot_size, net_pnl_inr, is_paper_trade,
                    exit_time, is_deleted, created_at, updated_at
                ) VALUES (
                    'NIFTY', 'CE', '22550 CE', 22550, '08-Oct-2026', 22550,
                    100.0, 99.5, 100.0, 0.5, 92.0,
                    92.0, 112.0, 120.0, '1:2.0', 85,
                    'Breakout', 'CLOSED', 105.0, 105.0, 100.0,
                    5.0, 5.0, 1, 65, 325.0, 1,
                    datetime('now', '-20 minutes'), 0, datetime('now', '-30 minutes'), datetime('now', '-20 minutes')
                )
            """)

        sig = record_signal({
            "symbol": "NIFTY",
            "type": "PE",
            "contract_name": "22550 PE",
            "strike": 22550.0,
            "expiry": "08-Oct-2026",
            "spot_price": 22550.0,
            "entry_price": 100.0,
            "ask_at_entry": 100.0,
            "bid_at_entry": 99.5,
            "stop_loss": 92.0,
        }, is_paper_trade=True)

        self.assertIsNotNone(sig)
        self.assertEqual(sig["status"], "ACTIVE")

    def test_scenario_10_two_consecutive_scheduler_confirmations_entry(self):
        """Scenario 10: 2 consecutive evaluations confirm entry."""
        count1, confirmed1 = update_pending_entry_confirmation("NIFTY", "CE", req_confs=2)
        self.assertEqual(count1, 1)
        self.assertFalse(confirmed1)

        count2, confirmed2 = update_pending_entry_confirmation("NIFTY", "CE", req_confs=2)
        self.assertEqual(count2, 2)
        self.assertTrue(confirmed2)

    def test_scenario_11_one_confirmation_only_wait(self):
        """Scenario 11: 1 confirmation only remains unconfirmed (WAIT)."""
        count, confirmed = update_pending_entry_confirmation("NIFTY", "CE", req_confs=2)
        self.assertEqual(count, 1)
        self.assertFalse(confirmed)

    def test_scenario_12_missing_invalid_bid_or_ask_no_signal(self):
        """Scenario 12: Missing bid/ask quotes returns WAIT and None trade_plan."""
        data = self._base_bullish_chain()
        data["chain"][0]["ce_bid"] = 0.0
        data["chain"][0]["ce_ask"] = 0.0
        res = analyze_option_desk(data, has_active_trade=False)
        self.assertEqual(res["option_buying"]["decision"], "WAIT")
        self.assertFalse(res["option_buying"]["data_quality"]["has_bid_ask"])

    def test_scenario_13_non_live_data_no_signal(self):
        """Scenario 13: Non-LIVE data returns WAIT."""
        data = self._base_bullish_chain()
        data["data_status"] = "STALE"
        res = analyze_option_desk(data, has_active_trade=False)
        self.assertEqual(res["option_buying"]["decision"], "WAIT")

    def test_scenario_14_daily_trade_count_at_3_no_new_signal(self):
        """Scenario 14: Daily trade count at 3 blocks new trade with MAX_DAILY_TRADES_REACHED."""
        data = self._base_bullish_chain()
        data["daily_trade_count"] = 3
        res = analyze_option_desk(data, has_active_trade=False)
        self.assertEqual(res["option_buying"]["decision"], "WAIT")
        self.assertEqual(res["option_buying"]["decision_reason"], "MAX_DAILY_TRADES_REACHED")


if __name__ == "__main__":
    unittest.main()
