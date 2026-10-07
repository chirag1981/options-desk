"""
tests/test_trade_analyzer_agent.py - Comprehensive Unit Test Suite for trade_analyzer_agent.py

Covers:
1. Empty trade journal (0 trades)
2. All-wins edge case
3. All-losses edge case
4. Missing / unparseable pillar flags (UNKNOWN handling)
5. Data quality exclusions (duration < 3m, zero IV, fabricated 100/50 quotes, invalid exit)
6. Sample size threshold (n < 30 labeled INSUFFICIENT_DATA vs n >= 30 DIAGNOSTIC_COMPLETE)
7. Wilson Score Interval & Bootstrap Confidence Interval bounds
8. Loser taxonomy classification (wrong direction, right-then-reversed, theta stagnation, stopped by noise)
9. Counterfactual parameter replay execution
10. Net statutory cost and R-multiple calculations
"""

import json
import unittest
from app.services.trade_analyzer_agent import (
    analyze_paper_trading_logs,
    _wilson_score_interval,
    _bootstrap_mean_ci,
    _bootstrap_profit_factor,
    _filter_data_quality,
    _enrich_trade_costs_and_r,
    _classify_losers,
    _run_counterfactual_replay,
    MIN_SAMPLE_SIZE,
)


class TestTradeAnalyzerAgent(unittest.TestCase):

    def setUp(self):
        # Sample base valid trade template
        self.base_trade = {
            "id": 1,
            "symbol": "NIFTY",
            "signal_type": "CE",
            "moneyness": "ATM",
            "entry_price": 150.0,
            "exit_price": 180.0,
            "sl_price": 127.5,
            "target_1": 180.0,
            "target_2": 210.0,
            "highest_price": 185.0,
            "lowest_price": 145.0,
            "duration_mins": 15.0,
            "iv_at_entry": 16.5,
            "setup_score": 85,
            "status": "TARGET_1_HIT",
            "exit_reason": "TARGET_1",
            "created_at": "2026-10-05 10:30:00",
            "quantity": 25,
            "points_pnl": 30.0,
            "net_pnl_inr": 700.0,
            "pillar_flags_json": json.dumps({
                "direction": True,
                "level": True,
                "momentum": True,
                "volume": True,
                "oi": True,
                "iv_session": True,
            }),
        }

    def test_empty_trades(self):
        """Edge Case 1: 0 trades in journal returns structured fallback with INSUFFICIENT_DATA."""
        report = analyze_paper_trading_logs(trades_override=[])
        self.assertEqual(report["status"], "INSUFFICIENT_DATA")
        self.assertEqual(report["sample_size"]["valid_closed_trades"], 0)
        self.assertFalse(report["sample_size"]["sample_sufficient"])
        self.assertIn("MFE and MAE metrics are captured via periodic", report["data_limitation_notice"])
        self.assertEqual(len(report["recommended_changes"]), 1)
        self.assertEqual(report["recommended_changes"][0]["evidence_status"], "INSUFFICIENT_DATA")

    def test_all_wins(self):
        """Edge Case 2: 100% win rate across 35 trades."""
        trades = []
        for i in range(35):
            t = dict(self.base_trade)
            t["id"] = i + 1
            t["entry_price"] = 150.0
            t["exit_price"] = 180.0 + (i % 5)
            t["highest_price"] = 190.0
            t["lowest_price"] = 148.0
            t["status"] = "CLOSED"
            trades.append(t)

        report = analyze_paper_trading_logs(trades_override=trades)
        self.assertEqual(report["status"], "DIAGNOSTIC_COMPLETE")
        self.assertEqual(report["sample_size"]["win_count"], 35)
        self.assertEqual(report["sample_size"]["loss_count"], 0)
        self.assertEqual(report["sample_size"]["win_rate_pct"], 100.0)
        self.assertGreater(report["overall_performance"]["expectancy_r"], 0.0)
        self.assertGreater(report["overall_performance"]["strategy_health_score"], 70)
        self.assertEqual(report["loser_taxonomy"]["total_losers"], 0)

    def test_all_losses(self):
        """Edge Case 3: 100% loss rate across 35 trades."""
        trades = []
        for i in range(35):
            t = dict(self.base_trade)
            t["id"] = i + 1
            t["entry_price"] = 150.0
            t["exit_price"] = 127.5
            t["sl_price"] = 127.5
            t["highest_price"] = 151.0
            t["lowest_price"] = 125.0
            t["status"] = "CLOSED"
            t["exit_reason"] = "SL_HIT"
            trades.append(t)

        report = analyze_paper_trading_logs(trades_override=trades)
        self.assertEqual(report["status"], "DIAGNOSTIC_COMPLETE")
        self.assertEqual(report["sample_size"]["win_count"], 0)
        self.assertEqual(report["sample_size"]["loss_count"], 35)
        self.assertEqual(report["sample_size"]["win_rate_pct"], 0.0)
        self.assertLess(report["overall_performance"]["expectancy_r"], 0.0)
        self.assertEqual(report["loser_taxonomy"]["total_losers"], 35)

    def test_missing_and_unparseable_pillar_flags(self):
        """Edge Case 4: Missing or invalid JSON in pillar flags marked as UNKNOWN."""
        trades = []
        for i in range(10):
            t = dict(self.base_trade)
            t["id"] = i + 1
            t["status"] = "CLOSED"
            if i < 4:
                t["pillar_flags_json"] = None  # Missing
            elif i < 7:
                t["pillar_flags_json"] = "{invalid_json_corrupted"  # Corrupted
            else:
                t["pillar_flags_json"] = json.dumps({"direction": True, "level": False})
            trades.append(t)

        report = analyze_paper_trading_logs(trades_override=trades)
        pillars = report["setup_pillars"]
        self.assertEqual(len(pillars), 6)
        dir_pillar = next(p for p in pillars if p["pillar_id"] == "direction")
        self.assertEqual(dir_pillar["unknown_count"], 7)
        self.assertEqual(dir_pillar["evaluated_trades"], 3)

    def test_data_quality_filter_exclusions(self):
        """Edge Case 5: Hard exclusions for fabricated 100/50 quotes and invalid exits. Missing/zero IV and short duration are soft-flagged but retained in core sample."""
        bad_trades = [
            # Short duration (< 3.0 min) - flagged but retained to avoid survivorship bias
            dict(self.base_trade, id=1, duration_mins=1.5),
            # Missing / zero IV - soft-flagged and retained as CORE_VALID
            dict(self.base_trade, id=2, iv_at_entry=0.0),
            dict(self.base_trade, id=3, iv_at_entry=None),
            # Fabricated default quote (100.0 or 50.0)
            dict(self.base_trade, id=4, entry_price=100.0),
            dict(self.base_trade, id=5, entry_price=50.0),
            # Invalid entry price <= 0.05
            dict(self.base_trade, id=6, entry_price=0.02),
            # Stale / zero exit price
            dict(self.base_trade, id=7, exit_price=0.0),
            # Valid trade
            dict(self.base_trade, id=8, duration_mins=10.0, entry_price=155.0, exit_price=175.0, iv_at_entry=15.2),
        ]

        valid, quality = _filter_data_quality(bad_trades)
        # Trades 1, 2, 3, 8 are CORE_VALID (4 trades)
        self.assertEqual(len(valid), 4)
        self.assertEqual(quality["core_valid_trades"], 4)
        self.assertEqual(quality["iv_valid_trades"], 2)  # Trade 1 & Trade 8
        self.assertEqual(quality["iv_missing_count"], 2)  # Trade 2 & Trade 3
        self.assertEqual(quality["excluded_count"], 4)  # Trades 4, 5, 6, 7
        self.assertEqual(quality["core_excluded_count"], 4)
        self.assertEqual(quality["exclusion_reasons"]["SHORT_DURATION_FLAGGED"], 1)
        self.assertEqual(quality["exclusion_reasons"]["MISSING_ZERO_IV_FLAGGED"], 2)
        self.assertEqual(quality["exclusion_reasons"]["FABRICATED_OR_OUTLIER_ENTRY"], 3)
        self.assertEqual(quality["exclusion_reasons"]["STALE_OR_INVALID_EXIT"], 1)

    def test_sample_size_threshold_insufficient_vs_sufficient(self):
        """Edge Case 6: n < 30 produces INSUFFICIENT_DATA; n >= 30 produces DIAGNOSTIC_COMPLETE."""
        # 10 trades -> Insufficient
        small_sample = [dict(self.base_trade, id=i+1, status="CLOSED") for i in range(10)]
        rep_small = analyze_paper_trading_logs(trades_override=small_sample)
        self.assertEqual(rep_small["status"], "INSUFFICIENT_DATA")
        self.assertFalse(rep_small["sample_size"]["sample_sufficient"])
        self.assertEqual(rep_small["recommended_changes"][0]["evidence_status"], "INSUFFICIENT_DATA")

        # 30 trades -> Sufficient
        large_sample = [dict(self.base_trade, id=i+1, status="CLOSED") for i in range(30)]
        rep_large = analyze_paper_trading_logs(trades_override=large_sample)
        self.assertEqual(rep_large["status"], "DIAGNOSTIC_COMPLETE")
        self.assertTrue(rep_large["sample_size"]["sample_sufficient"])

    def test_wilson_and_bootstrap_ci(self):
        """Edge Case 7: Statistical CI validity."""
        # Wilson interval test
        ci_zero = _wilson_score_interval(0, 30)
        self.assertAlmostEqual(ci_zero[0], 0.0, places=1)
        self.assertGreater(ci_zero[1], 0.0)

        ci_half = _wilson_score_interval(15, 30)
        self.assertLess(ci_half[0], 50.0)
        self.assertGreater(ci_half[1], 50.0)

        # Bootstrap mean CI test
        r_sample = [1.2, -0.8, 1.5, -1.0, 0.5, 2.0, -0.9, 1.1] * 5
        mean_ci = _bootstrap_mean_ci(r_sample, iterations=500)
        self.assertEqual(len(mean_ci), 2)
        self.assertLessEqual(mean_ci[0], mean_ci[1])

        # Bootstrap profit factor test
        pf_val, pf_ci = _bootstrap_profit_factor(r_sample, iterations=500)
        self.assertGreater(pf_val, 0.0)
        self.assertEqual(len(pf_ci), 2)
        self.assertLessEqual(pf_ci[0], pf_ci[1])

    def test_loser_taxonomy(self):
        """Edge Case 8: Loser taxonomy classification."""
        losers = [
            # 1. Wrong direction: never moved into profit (high == entry)
            dict(self.base_trade, entry_price=150.0, exit_price=130.0, sl_price=127.5, highest_price=150.0, lowest_price=125.0, duration_mins=10.0, net_pnl_pts=-20.0, r_multiple=-0.88),
            # 2. Right then reversed: reached 165 (+15 pts = +0.66R) but closed in loss
            dict(self.base_trade, entry_price=150.0, exit_price=135.0, sl_price=127.5, highest_price=165.0, lowest_price=130.0, duration_mins=20.0, net_pnl_pts=-15.0, r_multiple=-0.66),
            # 3. Theta stagnation: held 60 mins without hitting SL, eroded gradually
            dict(self.base_trade, entry_price=150.0, exit_price=140.0, sl_price=127.5, highest_price=152.0, lowest_price=138.0, duration_mins=65.0, net_pnl_pts=-10.0, r_multiple=-0.44),
            # 4. Stopped by noise: hit SL at 127.5, but high was 155.0 (exceeded entry)
            dict(self.base_trade, entry_price=150.0, exit_price=127.5, sl_price=127.5, highest_price=155.0, lowest_price=125.0, duration_mins=15.0, exit_reason="SL_HIT", net_pnl_pts=-22.5, r_multiple=-1.0),
        ]

        classified = _classify_losers(losers)
        self.assertEqual(classified["total_losers"], 4)
        self.assertEqual(classified["wrong_direction"]["count"], 1)
        self.assertEqual(classified["right_then_reversed"]["count"], 1)
        self.assertEqual(classified["theta_stagnation"]["count"], 1)
        self.assertEqual(classified["stopped_by_noise"]["count"], 1)

    def test_counterfactual_replay(self):
        """Edge Case 9: Counterfactual replay executes and calculates delta R with CIs."""
        trades = []
        for i in range(30):
            t = dict(self.base_trade)
            t["id"] = i + 1
            t["r_multiple"] = 0.5 if i % 2 == 0 else -0.5
            t["net_pnl_pts"] = 11.25 if i % 2 == 0 else -11.25
            t["status"] = "CLOSED"
            trades.append(t)

        counterfactual = _run_counterfactual_replay(trades)
        self.assertIn("SL_10_PCT", counterfactual)
        self.assertIn("SL_15_PCT_BASELINE", counterfactual)
        self.assertIn("T1_1_5_R_BASELINE", counterfactual)
        self.assertIn("TIME_STOP_45M", counterfactual)
        self.assertEqual(len(counterfactual["SL_10_PCT"]["delta_mean_r_95_ci"]), 2)

    def test_net_costs_and_r_multiples(self):
        """Edge Case 10: Calculates exact Indian statutory transaction costs and R-multiples."""
        raw_trade = {
            "symbol": "NIFTY",
            "quantity": 25,
            "entry_price": 100.0,
            "exit_price": 120.0,
            "sl_price": 85.0,
            "status": "CLOSED",
        }
        enriched = _enrich_trade_costs_and_r(raw_trade)
        # Gross pts = 20.0
        self.assertEqual(enriched["gross_pnl_pts"], 20.0)
        # Slippage: 100 * 0.002 + 120 * 0.002 = 0.20 + 0.24 = 0.44 pts
        self.assertAlmostEqual(enriched["slippage_pts"], 0.44, places=2)
        # Initial risk: 100 - 85 = 15.0 pts
        self.assertEqual(enriched["initial_risk_pts"], 15.0)
        # Net pts < Gross pts
        self.assertLess(enriched["net_pnl_pts"], enriched["gross_pnl_pts"])
        # R multiple = net_pnl_pts / 15.0
        self.assertAlmostEqual(enriched["r_multiple"], enriched["net_pnl_pts"] / 15.0, places=2)


if __name__ == "__main__":
    unittest.main()
