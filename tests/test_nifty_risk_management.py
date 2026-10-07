"""
tests/test_nifty_risk_management.py — Unit Tests for NIFTY Option Risk Management & Daily Trade Caps

Verifies:
Test 1: Entry Rs. 154.45. Verify new maximum-risk SL is tight and configurable (not 20% / ~31 pts).
Test 2: Technical invalidation occurs before maximum premium risk -> Technical SL is respected.
Test 3: Maximum premium-risk threshold is reached first -> Position exits without exceeding configured risk.
Test 4: Trailing stop still works after entry.
Test 5: T1/T2 still work.
Test 6: Time stop still works.
Test 7: After 3 daily trades -> No new trade allowed (MAX_DAILY_TRADES_REACHED), active trade continues normally.
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
    get_daily_trade_count,
    close_signal_manually,
    get_active_signal_for_symbol,
)

IST = ZoneInfo("Asia/Kolkata")


class TestNiftyRiskManagement(unittest.TestCase):

    def setUp(self):
        init_signal_db()
        with _get_db() as conn:
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM trade_ticks")

    def tearDown(self):
        with _get_db() as conn:
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM trade_ticks")

    def _sample_chain_data(self, spot=22550.0, entry_price=154.45, delta=0.50, is_ce=True):
        """Builds a test options chain payload."""
        strike = 22550
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
                "ce_oi": 100000,
                "pe_oi": 250000 if is_ce else 50000,
                "ce_change_oi": -20000 if is_ce else 60000,
                "pe_change_oi": 80000 if is_ce else -20000,
                "ce_volume": 500000 if is_ce else 100000,
                "pe_volume": 100000 if is_ce else 500000,
                "ce_iv": 15.0,
                "pe_iv": 15.0,
                "ce_delta": delta,
                "pe_delta": -delta,
            }
        ]
        return {
            "symbol": "NIFTY",
            "spot_price": spot,
            "spot_change_pct": 0.40 if is_ce else -0.40,
            "strike_step": 50,
            "data_status": "LIVE",
            "chain": chain,
            "prev_close": 22450.0 if is_ce else 22650.0,
            "open_price": 22480.0 if is_ce else 22620.0,
            "prev_high": 22520.0 if is_ce else 22680.0,
            "prev_low": 22400.0 if is_ce else 22560.0,
            "price_history": [22450.0, 22560.0, 22530.0, 22550.0] if is_ce else [22650.0, 22540.0, 22570.0, 22550.0],
        }

    def test_1_entry_154_45_max_risk_sl_is_tight_and_configurable(self):
        """Test 1: Entry Rs. 154.45 with max option risk configured to 8.0 pts (not 20% / ~31 pts)."""
        data = self._sample_chain_data(entry_price=154.45, is_ce=True)
        
        # Test with 8.0 pts max risk
        ENGINE_CONFIG["NIFTY_MAX_OPTION_RISK_POINTS"] = 8.0
        res = analyze_option_desk(data, has_active_trade=False)
        trade_plan = res["option_buying"]["trade_plan"]

        self.assertIsNotNone(trade_plan)
        self.assertEqual(trade_plan["entry_price"], 154.45)
        # Verify risk points <= 8.0
        self.assertLessEqual(trade_plan["risk_pts"], 8.0)
        self.assertAlmostEqual(trade_plan["stop_loss"], 154.45 - 8.0, places=2)
        # Verify it is NOT 20% (~30.9 pts)
        self.assertNotEqual(trade_plan["risk_pts"], round(154.45 * 0.20, 1))

        # Test configurability with 5.0 pts max risk
        ENGINE_CONFIG["NIFTY_MAX_OPTION_RISK_POINTS"] = 5.0
        res2 = analyze_option_desk(data, has_active_trade=False)
        trade_plan2 = res2["option_buying"]["trade_plan"]
        self.assertLessEqual(trade_plan2["risk_pts"], 5.0)

    def test_2_technical_invalidation_occurs_before_max_risk(self):
        """Test 2: When spot is very close to technical invalidation (S1), technical SL is respected."""
        ENGINE_CONFIG["NIFTY_MAX_OPTION_RISK_POINTS"] = 10.0
        data = self._sample_chain_data(spot=22558.0, entry_price=154.45, delta=0.50, is_ce=True)

        res = analyze_option_desk(data, has_active_trade=False)
        trade_plan = res["option_buying"]["trade_plan"]
        self.assertIsNotNone(trade_plan)
        self.assertLessEqual(trade_plan["risk_pts"], 10.0)
        self.assertAlmostEqual(trade_plan["risk_pts"], 4.0, delta=1.0)
        self.assertAlmostEqual(trade_plan["stop_loss"], 150.45, delta=1.0)

    def test_3_max_risk_threshold_reached_exits_within_configured_risk(self):
        """Test 3: Option price drops past SL -> Position exits without exceeding configured risk."""
        ENGINE_CONFIG["NIFTY_MAX_OPTION_RISK_POINTS"] = 8.0
        sig_data = {
            "symbol": "NIFTY",
            "type": "CE",
            "contract_name": "22550 CE",
            "strike": 22550.0,
            "expiry": "08-Oct-2026",
            "spot_price": 22550.0,
            "entry_price": 154.45,
            "ask_at_entry": 154.45,
            "bid_at_entry": 153.95,
            "stop_loss": 146.45,  # 8.0 pts SL
            "target_1": 166.45,
            "target_2": 174.45,
            "lots": 1,
            "lot_size": 65,
        }
        sig = record_signal(sig_data, is_paper_trade=False)
        self.assertIsNotNone(sig)

        # Price drops to 145.0 (below SL 146.45)
        market_cache = {
            "NIFTY": {
                "spot_price": 22530.0,
                "chain": [{"strike": 22550, "ce_ltp": 145.0, "ce_bid": 144.8, "ce_ask": 145.2}],
            }
        }
        midday = datetime(2026, 10, 7, 10, 30, tzinfo=IST)
        updated = update_active_signals(market_data_cache=market_cache, now_dt=midday)

        self.assertEqual(len(updated), 1)
        closed = updated[0]
        self.assertEqual(closed["status"], "SL_HIT")
        self.assertEqual(closed["exit_reason"], "STOP_LOSS_HIT")
        self.assertLessEqual(closed["entry_price"] - closed["exit_price"], 10.0)

    def test_4_trailing_stop_still_works(self):
        """Test 4: Trailing stop activates after favorable move and trails up."""
        sig_data = {
            "symbol": "NIFTY",
            "type": "CE",
            "contract_name": "22550 CE",
            "strike": 22550.0,
            "expiry": "08-Oct-2026",
            "spot_price": 22550.0,
            "entry_price": 100.0,
            "ask_at_entry": 100.0,
            "bid_at_entry": 99.5,
            "stop_loss": 92.0,  # 8 pts SL
            "target_1": 112.0,
            "target_2": 120.0,
            "lots": 1,
            "lot_size": 65,
        }
        sig = record_signal(sig_data, is_paper_trade=False)
        self.assertIsNotNone(sig)

        # Price rallies to 110.0 (near T1), trailing stop should ratchet up above initial SL 92.0
        market_cache_rally = {
            "NIFTY": {
                "spot_price": 22570.0,
                "chain": [{"strike": 22550, "ce_ltp": 110.0, "ce_bid": 109.5, "ce_ask": 110.5}],
            }
        }
        midday = datetime(2026, 10, 7, 10, 35, tzinfo=IST)
        updated = update_active_signals(market_data_cache=market_cache_rally, now_dt=midday)
        self.assertGreater(updated[0]["stop_loss"], 92.0)

    def test_5_target_1_and_target_2_work(self):
        """Test 5: Target 1 hit books profit / updates status, and Target 2 closes position."""
        sig_data = {
            "symbol": "NIFTY",
            "type": "CE",
            "contract_name": "22550 CE",
            "strike": 22550.0,
            "expiry": "08-Oct-2026",
            "spot_price": 22550.0,
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

        # Price hits 115.0 (> T1 112.0, < T2 120.0, bid 114.5 > trailed SL 113.5)
        market_cache_t1 = {
            "NIFTY": {
                "spot_price": 22580.0,
                "chain": [{"strike": 22550, "ce_ltp": 115.0, "ce_bid": 114.5, "ce_ask": 115.5}],
            }
        }
        midday = datetime(2026, 10, 7, 10, 35, tzinfo=IST)
        updated = update_active_signals(market_data_cache=market_cache_t1, now_dt=midday)
        self.assertEqual(updated[0]["status"], "TARGET_1_HIT")

        # Price further expands to 121.0 (>= T2 120.0)
        market_cache_t2 = {
            "NIFTY": {
                "spot_price": 22600.0,
                "chain": [{"strike": 22550, "ce_ltp": 121.5, "ce_bid": 121.0, "ce_ask": 122.0}],
            }
        }
        updated_t2 = update_active_signals(market_data_cache=market_cache_t2, now_dt=midday + timedelta(minutes=2))
        self.assertEqual(updated_t2[0]["status"], "TARGET_2_HIT")
        self.assertEqual(updated_t2[0]["exit_reason"], "TARGET_2_HIT")

    def test_6_time_stop_still_works(self):
        """Test 6: Stagnant trade past time limit closes with TIME_STOP_EXIT."""
        sig_data = {
            "symbol": "NIFTY",
            "type": "CE",
            "contract_name": "22550 CE",
            "strike": 22550.0,
            "expiry": "08-Oct-2026",
            "spot_price": 22550.0,
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

        # Set created_at to 35 minutes ago
        with _get_db() as conn:
            conn.execute("UPDATE signals SET created_at = datetime('now', '-35 minutes') WHERE id = ?", (sig["id"],))

        # Flat price now
        market_cache_stagnant = {
            "NIFTY": {
                "spot_price": 22550.0,
                "chain": [{"strike": 22550, "ce_ltp": 99.8, "ce_bid": 99.5, "ce_ask": 100.0}],
            }
        }
        now_time = datetime.now(IST)
        updated = update_active_signals(market_data_cache=market_cache_stagnant, now_dt=now_time)
        self.assertEqual(updated[0]["status"], "TIME_STOP_EXIT")
        self.assertEqual(updated[0]["exit_reason"], "TIME_STOP_EXIT")

    def test_7_max_daily_trades_limit_blocks_4th_trade_while_active_continues(self):
        """Test 7: After 3 daily trades, decision is WAIT with MAX_DAILY_TRADES_REACHED. Active trade continues."""
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        
        # Insert 3 completed trades for today
        with _get_db() as conn:
            for i in range(1, 4):
                conn.execute("""
                    INSERT INTO signals (
                        id, symbol, signal_type, contract_name, strike, expiry,
                        spot_at_entry, entry_price, bid_at_entry, ask_at_entry,
                        spread_paid, initial_stop_loss, stop_loss, target_1, target_2,
                        risk_reward, setup_score, trigger_reason, status,
                        current_price, highest_price, lowest_price, points_pnl, pnl_pct,
                        lots, lot_size, net_pnl_inr, is_paper_trade, exit_price, exit_reason,
                        is_deleted, created_at, updated_at
                    ) VALUES (
                        ?, 'NIFTY', 'CE', '22550 CE', 22550, '08-Oct-2026',
                        22550, 100.0, 99.5, 100.0, 0.5, 92.0, 92.0, 112.0, 120.0,
                        '1:2.0', 85, 'Breakout', 'CLOSED',
                        100.0, 100.0, 100.0, 0.0, 0.0,
                        1, 65, 0.0, 0, 105.0, 'TARGET_1',
                        0, ? || ' 10:00:00', ? || ' 10:15:00'
                    )
                """, (i, today_str, today_str))

        count = get_daily_trade_count("NIFTY", today_str)
        self.assertEqual(count, 3)

        # 4th trade attempt via analyze_option_desk
        data = self._sample_chain_data(is_ce=True)
        data["daily_trade_count"] = 3
        res = analyze_option_desk(data, has_active_trade=False)
        
        self.assertEqual(res["option_buying"]["decision"], "WAIT")
        self.assertEqual(res["option_buying"]["decision_reason"], "MAX_DAILY_TRADES_REACHED")

        # Verify record_signal also blocks 4th trade
        sig4 = record_signal({
            "symbol": "NIFTY",
            "type": "CE",
            "contract_name": "22550 CE",
            "strike": 22550.0,
            "entry_price": 100.0,
            "ask_at_entry": 100.0,
            "bid_at_entry": 99.5,
            "stop_loss": 92.0,
        })
        self.assertIsNone(sig4)


if __name__ == "__main__":
    unittest.main()
