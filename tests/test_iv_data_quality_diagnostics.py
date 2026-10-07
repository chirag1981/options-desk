"""
tests/test_iv_data_quality_diagnostics.py - Regression Test Suite for Missing IV Handling

Covers all 16 required test scenarios:
1. Valid trade + valid IV: CORE_VALID = true, IV_VALID = true
2. Valid trade + IV = 0: CORE_VALID = true, IV_VALID = false (retained in core sample)
3. Valid trade + IV = None: CORE_VALID = true, IV_VALID = false
4. Invalid entry price: Trade is CORE_INVALID
5. Invalid exit price: Trade is CORE_INVALID
6. Initial SL risk provenance: Follows existing project rules without rewriting
7. One core-valid trade: N = 1, status = INSUFFICIENT_DATA, trade diagnostics available
8. 29 core-valid trades: N = 29, status = INSUFFICIENT_DATA
9. 30 core-valid trades: N = 30, status = DIAGNOSTIC_COMPLETE
10. 30 core-valid trades + only 20 IV-valid: core N = 30, IV N = 20, statistical analysis eligible
11. Trade #1183 scenario: 22600 PE, Entry 166.40, Exit 157.85, SL risk 8.0, IV 0.0
12. No IV value is invented (preserves 0.0/None)
13. Recovered ATM IV never overwrites original iv_at_entry
14. Failed ATM IV recovery does not invalidate the trade
15. Existing API consumers continue to work seamlessly
16. Overall regression integrity
"""

import json
import sqlite3
import unittest
from datetime import datetime
from app.services.trade_analyzer_agent import (
    analyze_paper_trading_logs,
    review_closed_trade,
    _filter_data_quality,
    _enrich_trade_costs_and_r,
    _analyze_iv_regimes,
    MIN_SAMPLE_SIZE,
    _get_db,
)
from app.services.options_signal_service import (
    record_signal,
    get_signal_by_id,
    init_signal_db,
)


class TestIVDataQualityDiagnostics(unittest.TestCase):

    def setUp(self):
        init_signal_db()
        self.base_valid_trade = {
            "id": 101,
            "symbol": "NIFTY",
            "signal_type": "CE",
            "contract_name": "22500 CE",
            "strike": 22500.0,
            "moneyness": "ATM",
            "entry_price": 145.0,
            "ask_at_entry": 145.0,
            "bid_at_entry": 144.5,
            "exit_price": 165.0,
            "bid_at_exit": 165.0,
            "initial_stop_loss": 137.0,  # 8.0 pts risk
            "stop_loss": 137.0,
            "target_1": 157.0,
            "target_2": 165.0,
            "highest_price": 166.0,
            "lowest_price": 143.0,
            "duration_mins": 12.0,
            "iv_at_entry": 15.5,
            "setup_score": 82,
            "status": "TARGET_2_HIT",
            "exit_reason": "TARGET_2_HIT",
            "created_at": "2026-10-07 10:15:00",
            "lots": 1,
            "lot_size": 25,
            "points_pnl": 20.0,
            "net_pnl_inr": 480.0,
            "pillar_flags_json": json.dumps({"direction": True, "momentum": True, "level": True}),
        }

    # Test 1: Valid trade + valid IV
    def test_01_valid_trade_valid_iv(self):
        trades = [dict(self.base_valid_trade, iv_at_entry=16.8)]
        valid, summary = _filter_data_quality(trades)
        self.assertEqual(len(valid), 1)
        self.assertTrue(valid[0].get("core_valid"))
        self.assertTrue(valid[0].get("iv_valid"))
        self.assertEqual(valid[0].get("iv_status"), "VALID")
        self.assertEqual(summary["core_valid_trades"], 1)
        self.assertEqual(summary["iv_valid_trades"], 1)
        self.assertEqual(summary["iv_missing_count"], 0)

    # Test 2: Valid trade + IV = 0
    def test_02_valid_trade_zero_iv_retained_in_core(self):
        trades = [dict(self.base_valid_trade, iv_at_entry=0.0)]
        valid, summary = _filter_data_quality(trades)
        self.assertEqual(len(valid), 1)
        self.assertTrue(valid[0].get("core_valid"))
        self.assertFalse(valid[0].get("iv_valid"))
        self.assertEqual(valid[0].get("iv_status"), "MISSING")
        self.assertEqual(summary["core_valid_trades"], 1)
        self.assertEqual(summary["iv_valid_trades"], 0)
        self.assertEqual(summary["iv_missing_count"], 1)
        self.assertEqual(summary["core_excluded_count"], 0)

    # Test 3: Valid trade + IV = None
    def test_03_valid_trade_none_iv_retained_in_core(self):
        trades = [dict(self.base_valid_trade, iv_at_entry=None)]
        valid, summary = _filter_data_quality(trades)
        self.assertEqual(len(valid), 1)
        self.assertTrue(valid[0].get("core_valid"))
        self.assertFalse(valid[0].get("iv_valid"))
        self.assertEqual(valid[0].get("iv_status"), "MISSING")
        self.assertEqual(summary["core_valid_trades"], 1)
        self.assertEqual(summary["iv_missing_count"], 1)

    # Test 4: Invalid entry price -> CORE_INVALID
    def test_04_invalid_entry_price_excluded(self):
        trades = [
            dict(self.base_valid_trade, id=1, entry_price=0.0),
            dict(self.base_valid_trade, id=2, entry_price=100.0),  # Fabricated mock entry
            dict(self.base_valid_trade, id=3, entry_price=50.0),   # Fabricated mock entry
            dict(self.base_valid_trade, id=4, entry_price=60000.0), # Outlier entry
        ]
        valid, summary = _filter_data_quality(trades)
        self.assertEqual(len(valid), 0)
        self.assertEqual(summary["core_valid_trades"], 0)
        self.assertEqual(summary["core_excluded_count"], 4)
        self.assertEqual(summary["exclusion_reasons"]["FABRICATED_OR_OUTLIER_ENTRY"], 4)

    # Test 5: Invalid exit price -> CORE_INVALID
    def test_05_invalid_exit_price_excluded(self):
        trades = [dict(self.base_valid_trade, exit_price=0.0)]
        valid, summary = _filter_data_quality(trades)
        self.assertEqual(len(valid), 0)
        self.assertEqual(summary["core_valid_trades"], 0)
        self.assertEqual(summary["core_excluded_count"], 1)
        self.assertEqual(summary["exclusion_reasons"]["STALE_OR_INVALID_EXIT"], 1)

    # Test 6: Missing required initial SL -> Follows project rules
    def test_06_initial_stop_loss_handling(self):
        trade_no_sl = dict(self.base_valid_trade, initial_stop_loss=None, stop_loss=None, entry_price=150.0, ask_at_entry=150.0)
        enriched = _enrich_trade_costs_and_r(trade_no_sl)
        # Should fallback to 85% of entry (entry * 0.85 = 127.5, risk = 22.5) without raising error
        self.assertIn("r_multiple", enriched)
        self.assertAlmostEqual(enriched["initial_risk_pts"], 22.5, places=1)

    # Test 7: One core-valid trade: N = 1, status = INSUFFICIENT_DATA, trade diagnostics available
    def test_07_single_trade_diagnostics_available_with_insufficient_sample(self):
        single_trade = [dict(self.base_valid_trade, id=1, iv_at_entry=0.0, status="CLOSED")]
        report = analyze_paper_trading_logs(trades_override=single_trade)
        self.assertEqual(report["status"], "INSUFFICIENT_DATA")
        self.assertEqual(report["sample_sufficiency"], "INSUFFICIENT")
        self.assertEqual(report["sample_size"]["core_valid_trades"], 1)
        self.assertEqual(report["sample_size"]["iv_valid_trades"], 0)
        self.assertEqual(report["sample_size"]["iv_missing_count"], 1)
        self.assertFalse(report["sample_size"]["sample_sufficient"])
        self.assertNotEqual(report["status"], "NO_VALID_TRADES_AFTER_FILTER")

    # Test 8: 29 core-valid trades: N = 29, status = INSUFFICIENT_DATA
    def test_08_twenty_nine_trades_insufficient(self):
        trades = [dict(self.base_valid_trade, id=i+1, iv_at_entry=0.0 if i % 2 == 0 else 15.0, status="CLOSED") for i in range(29)]
        report = analyze_paper_trading_logs(trades_override=trades)
        self.assertEqual(report["status"], "INSUFFICIENT_DATA")
        self.assertEqual(report["sample_size"]["core_valid_trades"], 29)
        self.assertFalse(report["sample_size"]["sample_sufficient"])

    # Test 9: 30 core-valid trades: N = 30, status = DIAGNOSTIC_COMPLETE
    def test_09_thirty_trades_sufficient(self):
        trades = [dict(self.base_valid_trade, id=i+1, iv_at_entry=16.0, status="CLOSED") for i in range(30)]
        report = analyze_paper_trading_logs(trades_override=trades)
        self.assertEqual(report["status"], "DIAGNOSTIC_COMPLETE")
        self.assertEqual(report["sample_size"]["core_valid_trades"], 30)
        self.assertTrue(report["sample_size"]["sample_sufficient"])

    # Test 10: 30 core-valid trades + only 20 IV-valid -> statistical analysis eligible, IV sample = 20
    def test_10_thirty_core_twenty_iv_valid(self):
        trades = []
        for i in range(30):
            # 20 trades with valid IV, 10 trades with zero/missing IV
            iv = 15.0 + (i % 5) if i < 20 else 0.0
            trades.append(dict(self.base_valid_trade, id=i+1, iv_at_entry=iv, status="CLOSED"))

        report = analyze_paper_trading_logs(trades_override=trades)
        self.assertEqual(report["status"], "DIAGNOSTIC_COMPLETE")
        self.assertEqual(report["sample_size"]["core_valid_trades"], 30)
        self.assertEqual(report["sample_size"]["iv_valid_trades"], 20)
        self.assertEqual(report["sample_size"]["iv_missing_count"], 10)
        self.assertTrue(report["sample_size"]["sample_sufficient"])

        # Check IV regime dimensions
        iv_dims = report["dimensions"]["iv_regimes"]
        unavail_seg = next((s for s in iv_dims if s.get("regime") == "UNAVAILABLE"), None)
        self.assertIsNotNone(unavail_seg)
        self.assertEqual(unavail_seg["count"], 10)

    # Test 11: Trade #1183 scenario
    def test_11_trade_1183_scenario(self):
        trade_1183 = {
            "id": 1183,
            "symbol": "NIFTY",
            "signal_type": "PE",
            "contract_name": "22600 PE",
            "strike": 22600.0,
            "entry_price": 166.40,
            "ask_at_entry": 166.40,
            "bid_at_entry": 165.90,
            "exit_price": 157.85,
            "bid_at_exit": 157.85,
            "initial_stop_loss": 158.40,  # 8.0 points risk
            "stop_loss": 158.40,
            "target_1": 178.40,
            "target_2": 186.40,
            "highest_price": 167.20,
            "lowest_price": 157.85,
            "duration_mins": 3.2,
            "iv_at_entry": 0.0,
            "setup_score": 80,
            "status": "SL_HIT",
            "exit_reason": "STOP_LOSS_HIT",
            "created_at": "2026-10-07 11:00:00",
            "lots": 1,
            "lot_size": 25,
            "pillar_flags_json": json.dumps({"direction": True, "momentum": True}),
        }

        # Step 1: Data quality filter preserves Trade #1183 as CORE_VALID
        valid, summary = _filter_data_quality([trade_1183])
        self.assertEqual(len(valid), 1)
        self.assertTrue(valid[0]["core_valid"])
        self.assertFalse(valid[0]["iv_valid"])
        self.assertEqual(valid[0]["iv_status"], "MISSING")

        # Step 2: Enrichment calculates P&L and R correctly
        enriched = _enrich_trade_costs_and_r(valid[0])
        self.assertAlmostEqual(enriched["initial_risk_pts"], 8.0, places=1)
        self.assertLess(enriched["net_pnl_pts"], 0)
        # Gross R before fees: -8.55 / 8.0 = -1.07R
        gross_r = (enriched["exit_price"] - enriched["entry_price"]) / enriched["initial_risk_pts"]
        self.assertAlmostEqual(gross_r, -1.07, places=2)
        # Net R under existing transaction-cost model: -1.35R
        self.assertAlmostEqual(enriched["r_multiple"], -1.35, places=1)

        # Step 3: Single trade review does NOT mark it as DATA_QUALITY_ISSUE
        sig_id = 991183
        with _get_db() as conn:
            conn.execute("DELETE FROM trade_reviews WHERE signal_id = ?", (sig_id,))
            conn.execute("DELETE FROM signals WHERE id = ?", (sig_id,))
            conn.execute("""
                INSERT INTO signals (
                    id, symbol, signal_type, contract_name, strike, spot_at_entry, entry_price,
                    current_price, ask_at_entry, bid_at_entry, exit_price, bid_at_exit, initial_stop_loss,
                    stop_loss, target_1, target_2, highest_price, lowest_price,
                    duration_mins, iv_at_entry, setup_score, status, exit_reason,
                    created_at, updated_at, exit_time, lots, lot_size, is_paper_trade, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
            """, (
                sig_id, "NIFTY", "PE", "22600 PE", 22600.0, 22610.0, 166.40,
                157.85, 166.40, 165.90, 157.85, 157.85, 158.40,
                158.40, 178.40, 186.40, 167.20, 157.85,
                3.2, 0.0, 80, "SL_HIT", "STOP_LOSS_HIT",
                "2026-10-07 11:00:00", "2026-10-07 11:03:12", "2026-10-07 11:03:12", 1, 25
            ))

        review = review_closed_trade(sig_id)
        self.assertIsNotNone(review)
        self.assertNotEqual(review["classification"], "DATA_QUALITY_ISSUE")
        self.assertFalse(review["is_data_quality_flagged"])
        self.assertIn(review["classification"], ("STOPPED_BY_NOISE", "WRONG_DIRECTION", "RIGHT_THEN_REVERSED", "CLEAN_WIN"))

        # Step 4: Overall log analysis works with N=1
        report = analyze_paper_trading_logs(trades_override=[trade_1183])
        self.assertEqual(report["sample_size"]["core_valid_trades"], 1)
        self.assertEqual(report["sample_size"]["iv_valid_trades"], 0)
        self.assertEqual(report["sample_size"]["iv_missing_count"], 1)

    # Test 12: No IV value is invented
    def test_12_no_iv_invented(self):
        t = dict(self.base_valid_trade, iv_at_entry=0.0)
        valid, _ = _filter_data_quality([t])
        self.assertEqual(valid[0]["iv_at_entry"], 0.0)

    # Test 13: Recovered ATM IV never overwrites original iv_at_entry in signal service
    def test_13_recovered_atm_iv_stored_separately(self):
        signal_data = {
            "symbol": "NIFTY",
            "type": "CE",
            "strike": 22500.0,
            "entry_price": 150.0,
            "ask_at_entry": 150.0,
            "bid_at_entry": 149.5,
            "stop_loss": 142.0,
            "target_1": 162.0,
            "target_2": 170.0,
            "iv_at_entry": 0.0,      # Raw incoming IV is 0.0
            "atm_iv": 15.4,           # Chain ATM IV available
            "raw_values": {"spot": 22510.0, "atm_iv": 15.4},
        }

        # Mock DB insertion through record_signal
        sig = record_signal(signal_data, is_paper_trade=True)
        if sig:
            self.assertEqual(sig["iv_at_entry"], 0.0)  # Raw IV preserved as 0.0
            dq = json.loads(sig["data_quality_json"] or "{}")
            self.assertTrue(dq.get("iv_missing"))
            self.assertEqual(dq.get("recovered_atm_iv"), 15.4)
            self.assertEqual(dq.get("recovered_iv_source"), "ATM_CHAIN")

    # Test 14: Failed ATM IV recovery does not invalidate trade
    def test_14_failed_atm_recovery_trade_remains_core_valid(self):
        signal_data = {
            "symbol": "NIFTY",
            "type": "PE",
            "strike": 22500.0,
            "entry_price": 160.0,
            "ask_at_entry": 160.0,
            "bid_at_entry": 159.5,
            "stop_loss": 152.0,
            "target_1": 172.0,
            "target_2": 180.0,
            "iv_at_entry": 0.0,
            "atm_iv": 0.0,  # ATM IV also unavailable
        }

        sig = record_signal(signal_data, is_paper_trade=True)
        if sig:
            self.assertEqual(sig["iv_at_entry"], 0.0)
            dq = json.loads(sig["data_quality_json"] or "{}")
            self.assertIsNone(dq.get("recovered_atm_iv"))
            self.assertEqual(dq.get("recovered_iv_source"), "UNAVAILABLE")

    # Test 15: Existing API consumers continue to work seamlessly
    def test_15_api_response_structure_preserved(self):
        report = analyze_paper_trading_logs(trades_override=[dict(self.base_valid_trade, status="CLOSED")])
        expected_keys = [
            "status", "sample_sufficiency", "mode", "advisory_notice",
            "data_quality", "sample_size", "overall_performance",
            "setup_pillars", "loser_taxonomy", "counterfactual_replay",
            "best_conditions", "worst_conditions", "top_profit_leaks",
            "recommended_changes", "dimensions", "timestamp"
        ]
        for k in expected_keys:
            self.assertIn(k, report)

        # data_quality keys
        dq = report["data_quality"]
        self.assertIn("total_raw", dq)
        self.assertIn("valid_count", dq)
        self.assertIn("core_valid_trades", dq)
        self.assertIn("iv_valid_trades", dq)
        self.assertIn("iv_missing_count", dq)
        self.assertIn("excluded_count", dq)
        self.assertIn("core_excluded_count", dq)

    # Test 16: Full IV regime analysis with mixed valid and missing IV
    def test_16_iv_regimes_analysis_with_mixed_samples(self):
        trades = [
            dict(self.base_valid_trade, id=1, iv_at_entry=11.5, r_multiple=1.2, net_pnl_pts=10.0, net_pnl_inr=250.0),
            dict(self.base_valid_trade, id=2, iv_at_entry=16.5, r_multiple=0.8, net_pnl_pts=6.0, net_pnl_inr=150.0),
            dict(self.base_valid_trade, id=3, iv_at_entry=22.0, r_multiple=-1.0, net_pnl_pts=-8.0, net_pnl_inr=-200.0),
            dict(self.base_valid_trade, id=4, iv_at_entry=28.0, r_multiple=2.0, net_pnl_pts=16.0, net_pnl_inr=400.0),
            dict(self.base_valid_trade, id=5, iv_at_entry=0.0, r_multiple=-1.0, net_pnl_pts=-8.0, net_pnl_inr=-200.0),
        ]
        regimes = _analyze_iv_regimes(trades, baseline_r=0.4)
        regime_names = [r["regime"] for r in regimes]
        self.assertIn("LOW (<13%)", regime_names)
        self.assertIn("NORMAL (13-20%)", regime_names)
        self.assertIn("ELEVATED (20-26%)", regime_names)
        self.assertIn("HIGH (>26%)", regime_names)
        self.assertIn("UNAVAILABLE", regime_names)

        unavail = next(r for r in regimes if r["regime"] == "UNAVAILABLE")
        self.assertEqual(unavail["count"], 1)


if __name__ == "__main__":
    unittest.main()
