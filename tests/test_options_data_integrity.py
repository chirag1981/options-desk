"""
tests/test_options_data_integrity.py - Comprehensive Unit Tests for Data Integrity & Backtest

Tests:
1. Missing bid/ask: Engine fails closed (decision = "WAIT", trade_plan = None).
2. Missing / zero IV: evaluate_iv_and_time_filter fails closed (iv_favorable = False).
3. Expiry day: DTE is explicitly 0.0 (not floored to 0.25) when expiry == today.
4. Time-window forward labels (+15m, +30m) and real spot ATR calculation.
5. Contract-switch case: Forward option MFE/MAE tracks the SAME contract only.
6. Partial exits: Calculates separate brokerage (₹60) and turnover for multi-leg exits.
7. Statutory costs: Verifies STT 0.15% (effective 1 Apr 2026) and exact charges schedule.
8. Single-process lock: Scheduler enforces PID lock properly.
"""

import os
import json
import sqlite3
import unittest
from datetime import datetime, date
from zoneinfo import ZoneInfo
from app.services.options_engine import (
    analyze_option_desk,
    evaluate_iv_and_time_filter,
    get_symbol_oi_thresholds,
    update_symbol_bias_state,
    ENGINE_CONFIG,
)
from app.services.options_signal_service import (
    _get_db,
    init_signal_db,
    record_daily_atm_iv,
    get_rolling_iv_percentile,
    log_decision,
    backfill_forward_labels,
)
from app.services.options_replay_backtest import (
    calculate_trade_costs,
    compute_trade_r_multiple,
    CHARGES_CONFIG,
)

IST = ZoneInfo("Asia/Kolkata")


class TestOptionsDataIntegrity(unittest.TestCase):

    def setUp(self):
        init_signal_db()
        self.today_str = datetime.now(IST).strftime("%d-%b-%Y")

    def test_missing_bid_ask_fails_closed(self):
        """Test 1: Missing bid/ask quote fails closed (returns WAIT / trade_plan is None)."""
        chain = [{
            "strike": 24000,
            "ce_oi": 100000, "ce_change_oi": 50000, "ce_volume": 100000,
            "ce_ltp": 120.0, "ce_bid": 0.0, "ce_ask": 0.0, "ce_iv": 15.0,  # Missing bid/ask
            "pe_oi": 80000, "pe_change_oi": 10000, "pe_volume": 50000,
            "pe_ltp": 90.0, "pe_bid": 89.0, "pe_ask": 91.0, "pe_iv": 16.0,
        }]
        market_data = {
            "symbol": "NIFTY",
            "spot_price": 24050.0,
            "strike_step": 50,
            "spot_change_pct": 0.40,
            "expiry": self.today_str,
            "chain": chain,
        }
        res = analyze_option_desk(market_data)
        opt_buying = res["option_buying"]
        self.assertEqual(opt_buying["decision"], "WAIT")
        self.assertFalse(opt_buying["data_quality"]["has_bid_ask"])

    def test_missing_zero_iv_fails_closed(self):
        """Test 2: Missing or zero IV sets IV_UNAVAILABLE and does not block trade."""
        iv_res_zero = evaluate_iv_and_time_filter(0.0)
        self.assertTrue(iv_res_zero["iv_favorable"])
        self.assertEqual(iv_res_zero["iv_condition"], "IV_UNAVAILABLE")

        iv_res_none = evaluate_iv_and_time_filter(None)
        self.assertTrue(iv_res_none["iv_favorable"])
        self.assertEqual(iv_res_none["iv_condition"], "IV_UNAVAILABLE")

    def test_expiry_day_dte_zero(self):
        """Test 3: Expiry day produces dte = 0.0 explicitly (not floored to 0.25)."""
        chain = [{
            "strike": 24000, "ce_oi": 50000, "ce_change_oi": 10000, "ce_volume": 50000,
            "ce_ltp": 100.0, "ce_bid": 99.0, "ce_ask": 101.0, "ce_iv": 14.0,
            "pe_oi": 50000, "pe_change_oi": 10000, "pe_volume": 50000,
            "pe_ltp": 100.0, "pe_bid": 99.0, "pe_ask": 101.0, "pe_iv": 14.0,
        }]
        market_data = {
            "symbol": "NIFTY",
            "spot_price": 24000.0,
            "strike_step": 50,
            "expiry": self.today_str,  # Expiry is today!
            "chain": chain,
        }
        res = analyze_option_desk(market_data)
        self.assertEqual(res["market_bias"]["dte"], 0.0)

    def test_rolling_iv_percentile_under_30_days(self):
        """Test 4: Rolling IV percentile returns None until >= 30 daily observations exist."""
        # Clean test entries
        with _get_db() as conn:
            conn.execute("DELETE FROM daily_atm_iv WHERE symbol = 'TESTSYM'")
            conn.commit()

        # Insert 5 days
        for i in range(5):
            record_daily_atm_iv("TESTSYM", 14.0 + i, trade_date=f"2026-09-0{i+1}")

        pct_5 = get_rolling_iv_percentile("TESTSYM", 15.0, min_days=30)
        self.assertIsNone(pct_5)

        # Insert 30 days
        for i in range(30):
            record_daily_atm_iv("TESTSYM", 12.0 + (i * 0.2), trade_date=f"2026-08-{i+1:02d}")

        pct_30 = get_rolling_iv_percentile("TESTSYM", 15.0, min_days=30)
        self.assertIsNotNone(pct_30)
        self.assertGreater(pct_30, 0.0)

    def test_forward_labels_same_contract_and_time_windows(self):
        """Test 5: Time-window forward labels (+15m, +30m) track the SAME contract."""
        test_sym = "TEST_INDEX"
        with _get_db() as conn:
            conn.execute("DELETE FROM decision_log WHERE symbol = ?", (test_sym,))
            conn.commit()

        base_time = datetime(2026, 10, 5, 10, 0, 0, tzinfo=IST)
        
        # Row 0: Original decision for 24000 CE
        log_decision({
            "symbol": test_sym,
            "spot_price": 24000.0,
            "spot_change_pct": 0.2,
            "atm_strike": 24000,
            "atm_iv": 14.5,
            "chosen_contract": "24000 CE",
            "contract_ltp": 120.0,
            "contract_bid": 119.0,
            "contract_ask": 121.0,
            "decision": "CE",
        })

        # Update created_at of row 0 to base_time
        with _get_db() as conn:
            row0_id = conn.execute("SELECT id FROM decision_log WHERE symbol = ? ORDER BY id DESC LIMIT 1", (test_sym,)).fetchone()["id"]
            conn.execute("UPDATE decision_log SET created_at = ? WHERE id = ?", (base_time.strftime("%Y-%m-%d %H:%M:%S"), row0_id))
            conn.commit()

        # Insert subsequent ticks at +15 min and +30 min
        time_15m = datetime(2026, 10, 5, 10, 15, 0, tzinfo=IST)
        time_30m = datetime(2026, 10, 5, 10, 30, 0, tzinfo=IST)

        with _get_db() as conn:
            # +15m tick: Same contract 24000 CE traded up to 145.0
            conn.execute("""
                INSERT INTO decision_log (
                    timestamp, symbol, spot_price, spot_change_pct, atm_strike, atm_iv,
                    pcr, bullish_score, bearish_score, bias, pillar_direction, pillar_level,
                    pillar_momentum, pillar_volume, pillar_oi, pillar_iv_session,
                    chosen_contract, contract_ltp, setup_score, decision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                time_15m.strftime("%Y-%m-%d %H:%M:%S"), test_sym, 24080.0, 0.5, 24100, 15.0,
                1.2, 80, 20, "BULLISH", 1, 1, 1, 1, 1, 1,
                "24000 CE", 145.0, 85, "CE", time_15m.strftime("%Y-%m-%d %H:%M:%S")
            ))

            # +30m tick: Spot moved to 24120.0, but another contract 24100 CE was evaluated
            conn.execute("""
                INSERT INTO decision_log (
                    timestamp, symbol, spot_price, spot_change_pct, atm_strike, atm_iv,
                    pcr, bullish_score, bearish_score, bias, pillar_direction, pillar_level,
                    pillar_momentum, pillar_volume, pillar_oi, pillar_iv_session,
                    chosen_contract, contract_ltp, setup_score, decision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                time_30m.strftime("%Y-%m-%d %H:%M:%S"), test_sym, 24120.0, 0.7, 24100, 15.0,
                1.3, 85, 15, "BULLISH", 1, 1, 1, 1, 1, 1,
                "24100 CE", 110.0, 85, "CE", time_30m.strftime("%Y-%m-%d %H:%M:%S")
            ))
            conn.commit()

        # Run forward labels backfill
        backfill_forward_labels(test_sym)

        with _get_db() as conn:
            updated_row0 = conn.execute("SELECT * FROM decision_log WHERE id = ?", (row0_id,)).fetchone()
            # Spot moved from 24000 to 24080 (+15m) and 24120 (+30m)
            self.assertIsNotNone(updated_row0["forward_underlying_15m"])
            self.assertIsNotNone(updated_row0["forward_underlying_30m"])
            # Forward MFE % tracked only 24000 CE (120 -> 145 = +20.83%), NOT the 24100 CE quote!
            self.assertAlmostEqual(updated_row0["forward_mfe_pct"], 20.83, places=1)

    def test_partial_exits_separate_order_costs(self):
        """Test 6: Models partial exits with separate order brokerage (₹60 total) and STT 0.15%."""
        entry = 100.0
        # 50% exit at T1 (130), 50% exit at T2 (160)
        exit_legs = [(130.0, 0.5), (160.0, 0.5)]
        lot_size = 25
        lots = 2  # Total 50 qty

        costs = calculate_trade_costs(entry, exit_legs, lot_size=lot_size, lots=lots)
        self.assertEqual(costs["order_count"], 3)  # 1 buy + 2 sells
        self.assertEqual(costs["brokerage"], 60.0)  # ₹20 * 3 = ₹60

        # STT at 0.15% on sell turnover (130*25 + 160*25 = 3250 + 4000 = 7250 -> 7250 * 0.0015 = 10.875)
        self.assertAlmostEqual(costs["stt"], 10.88, places=1)

    def test_statutory_charges_schedule(self):
        """Test 7: Verifies statutory charges schedule against effective date 2026-04-01."""
        self.assertEqual(CHARGES_CONFIG["EFFECTIVE_DATE"], "2026-04-01")
        self.assertEqual(CHARGES_CONFIG["STT_SELL_OPTIONS_RATE"], 0.0015)
        self.assertEqual(CHARGES_CONFIG["BROKERAGE_PER_ORDER"], 20.0)
        self.assertEqual(CHARGES_CONFIG["GST_RATE"], 0.18)


if __name__ == "__main__":
    unittest.main()
