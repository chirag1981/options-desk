"""
tests/test_trade_review_system.py - Unit Test Suite for Automated Post-Trade Review System

Covers:
1. Idempotency: review_closed_trade produces 1 row per signal_id.
2. Non-blocking failure: reviewer exceptions do not prevent trade closing.
3. 7 mutually exclusive classifications:
   - WRONG_DIRECTION
   - RIGHT_THEN_REVERSED
   - STOPPED_BY_NOISE
   - THETA_STAGNATION
   - CLEAN_WIN
   - EARLY_EXIT
   - DATA_QUALITY_ISSUE
4. Counterfactual calculations on stored path.
5. Hypothesis promotion thresholds (n >= 30, CI excludes 0, walk-forward validation).
6. Exclusion of DATA_QUALITY_ISSUE trades from hypothesis promotion.
7. Startup catch-up job for unreviewed closed trades.
"""

import unittest
import json
import sqlite3
from unittest.mock import patch, MagicMock
from app.services.options_signal_service import (
    _get_db,
    init_signal_db,
    record_signal,
    update_active_signals,
    close_signal_manually,
    trigger_async_trade_review,
)
from app.services.trade_analyzer_agent import (
    review_closed_trade,
    review_unreviewed_closed_trades,
    update_hypotheses_from_review,
    generate_daily_review_report,
)


class TestTradeReviewSystem(unittest.TestCase):

    def setUp(self):
        init_signal_db()
        with _get_db() as conn:
            conn.execute("DELETE FROM trade_ticks")
            conn.execute("DELETE FROM trade_reviews")
            conn.execute("DELETE FROM hypotheses")
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM decision_log")

    def _insert_test_signal(self, **kwargs) -> int:
        defaults = {
            "symbol": "NIFTY",
            "strike": 24000,
            "signal_type": "CE",
            "contract_name": "24000 CE",
            "spot_at_entry": 24000.0,
            "entry_price": 100.0,
            "current_price": 100.0,
            "bid_at_entry": 99.8,
            "ask_at_entry": 100.2,
            "stop_loss": 85.0,
            "target_1": 130.0,
            "target_2": 150.0,
            "status": "ACTIVE",
            "is_paper_trade": 1,
            "lots": 1,
            "lot_size": 25,
            "duration_mins": 15.0,
            "highest_price": 100.0,
            "lowest_price": 100.0,
            "setup_score": 80,
            "iv_at_entry": 16.5,
            "data_quality_json": json.dumps({"dte": 3.0}),
            "pillar_flags_json": json.dumps({
                "direction": True,
                "level": True,
                "momentum": True,
                "volume": True,
                "oi": True,
                "iv_session": True,
            }),
            "created_at": "2026-10-05 09:30:00",
            "updated_at": "2026-10-05 09:45:00",
        }
        defaults.update(kwargs)
        with _get_db() as conn:
            cursor = conn.cursor()
            cols = list(defaults.keys())
            placeholders = ", ".join("?" for _ in cols)
            col_names = ", ".join(cols)
            cursor.execute(f"INSERT INTO signals ({col_names}) VALUES ({placeholders})", [defaults[k] for k in cols])
            return cursor.lastrowid

    def test_idempotency(self):
        """Reviewing the same closed signal multiple times produces exactly one review row."""
        sig_id = self._insert_test_signal(
            status="SL_HIT",
            exit_reason="SL_HIT",
            exit_price=85.0,
            bid_at_exit=84.8,
            ask_at_exit=85.2,
            lowest_price=84.0,
            highest_price=101.0,
            duration_mins=10.0,
        )

        review1 = review_closed_trade(sig_id)
        self.assertIsNotNone(review1)
        self.assertEqual(review1["signal_id"], sig_id)

        review2 = review_closed_trade(sig_id)
        self.assertIsNotNone(review2)
        self.assertEqual(review1["id"], review2["id"])

        with _get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM trade_reviews WHERE signal_id = ?", (sig_id,)).fetchone()[0]
            self.assertEqual(count, 1)

    def test_non_blocking_failure_on_manual_close(self):
        """Exceptions during trade review do not prevent closing a signal."""
        sig_id = self._insert_test_signal(status="ACTIVE")

        # Mock review_closed_trade to raise an exception
        with patch("app.services.trade_analyzer_agent.review_closed_trade", side_effect=RuntimeError("Review DB Error")):
            res = close_signal_manually(sig_id, exit_price=110.0, reason="MANUAL_TEST")
            self.assertTrue(res)

        with _get_db() as conn:
            sig = conn.execute("SELECT status, exit_reason FROM signals WHERE id = ?", (sig_id,)).fetchone()
            self.assertEqual(sig["status"], "CLOSED")
            self.assertEqual(sig["exit_reason"], "MANUAL_TEST")

    def test_classification_wrong_direction(self):
        """Trade never entered meaningful profit (highest <= entry + 0.05R)."""
        sig_id = self._insert_test_signal(
            status="SL_HIT",
            exit_reason="SL_HIT",
            entry_price=100.0,
            exit_price=85.0,
            stop_loss=85.0,
            highest_price=100.2,
            lowest_price=84.5,
            duration_mins=12.0,
        )
        review = review_closed_trade(sig_id)
        self.assertEqual(review["classification"], "WRONG_DIRECTION")
        self.assertLessEqual(review["mfe_capture_pct"], 5.0)

    def test_classification_right_then_reversed(self):
        """Trade reached > 0.5R profit (e.g. 110 with initial risk 15) but subsequently reversed to loss."""
        sig_id = self._insert_test_signal(
            status="SL_HIT",
            exit_reason="SL_HIT",
            entry_price=100.0,
            exit_price=85.0,
            stop_loss=85.0,
            highest_price=112.0,  # +12 pts = +0.8R
            lowest_price=84.5,
            duration_mins=25.0,
        )
        review = review_closed_trade(sig_id)
        self.assertEqual(review["classification"], "RIGHT_THEN_REVERSED")

    def test_classification_stopped_by_noise(self):
        """Trade hit SL, but post-exit contract ticks in decision_log recovered to Target 1."""
        sig_id = self._insert_test_signal(
            status="SL_HIT",
            exit_reason="SL_HIT",
            entry_price=100.0,
            exit_price=85.0,
            stop_loss=85.0,
            target_1=130.0,
            highest_price=102.0,
            lowest_price=84.5,
            duration_mins=8.0,
            updated_at="2026-10-05 10:00:00",
        )
        # Log post-exit recovery in decision_log
        with _get_db() as conn:
            conn.execute("""
                INSERT INTO decision_log (
                    timestamp, symbol, spot_price, spot_change_pct, atm_strike, atm_iv, pcr,
                    bullish_score, bearish_score, bias, setup_score, decision,
                    pillar_direction, pillar_level, pillar_momentum, pillar_volume, pillar_oi, pillar_iv_session,
                    chosen_contract, contract_ltp, created_at
                ) VALUES (
                    '2026-10-05 10:15:00', 'NIFTY', 24000.0, 0.5, 24000.0, 15.0, 1.1,
                    70.0, 30.0, 'BULLISH', 80, 'WAIT',
                    1, 1, 1, 1, 1, 1,
                    '24000 CE', 132.0, '2026-10-05 10:15:00'
                )
            """)

        review = review_closed_trade(sig_id)
        self.assertEqual(review["classification"], "STOPPED_BY_NOISE")

    def test_classification_theta_stagnation(self):
        """Trade held for > 45 minutes with low excursion, ending in loss."""
        sig_id = self._insert_test_signal(
            status="TIME_STOP_EXIT",
            exit_reason="TIME_STOP_EXIT",
            entry_price=100.0,
            exit_price=92.0,
            stop_loss=85.0,
            highest_price=103.0,
            lowest_price=91.0,
            duration_mins=55.0,
        )
        review = review_closed_trade(sig_id)
        self.assertEqual(review["classification"], "THETA_STAGNATION")

    def test_classification_clean_win(self):
        """Trade hit Target 1 or Target 2 with minimal drawdown (MAE <= 0.5R)."""
        sig_id = self._insert_test_signal(
            status="TARGET_2_HIT",
            exit_reason="TARGET_2_HIT",
            entry_price=100.0,
            exit_price=150.0,
            stop_loss=85.0,
            target_1=130.0,
            target_2=150.0,
            highest_price=152.0,
            lowest_price=96.0,  # Drawdown 4 pts vs 15 pts risk = 0.26R
            duration_mins=20.0,
        )
        review = review_closed_trade(sig_id)
        self.assertEqual(review["classification"], "CLEAN_WIN")
        self.assertGreater(review["r_multiple"], 1.0)

    def test_classification_early_exit(self):
        """Target 1 hit, but contract ran much further in post-exit tracking."""
        sig_id = self._insert_test_signal(
            status="TARGET_1_HIT",
            exit_reason="TARGET_1_HIT",
            entry_price=100.0,
            exit_price=130.0,
            stop_loss=85.0,
            target_1=130.0,
            target_2=150.0,
            highest_price=132.0,
            lowest_price=98.0,
            duration_mins=15.0,
            updated_at="2026-10-05 10:30:00",
        )
        with _get_db() as conn:
            conn.execute("""
                INSERT INTO decision_log (
                    timestamp, symbol, spot_price, spot_change_pct, atm_strike, atm_iv, pcr,
                    bullish_score, bearish_score, bias, setup_score, decision,
                    pillar_direction, pillar_level, pillar_momentum, pillar_volume, pillar_oi, pillar_iv_session,
                    chosen_contract, contract_ltp, created_at
                ) VALUES (
                    '2026-10-05 10:45:00', 'NIFTY', 24000.0, 0.5, 24000.0, 15.0, 1.1,
                    70.0, 30.0, 'BULLISH', 80, 'WAIT',
                    1, 1, 1, 1, 1, 1,
                    '24000 CE', 190.0, '2026-10-05 10:45:00'
                )
            """)

        review = review_closed_trade(sig_id)
        self.assertEqual(review["classification"], "EARLY_EXIT")

    def test_classification_data_quality_issue(self):
        """Trades with zero IV or missing quotes receive DATA_QUALITY_ISSUE classification."""
        sig_id = self._insert_test_signal(
            status="SL_HIT",
            exit_reason="SL_HIT",
            entry_price=100.0,
            exit_price=85.0,
            iv_at_entry=0.0,  # Zero IV
            duration_mins=2.0,  # Short duration
        )
        review = review_closed_trade(sig_id)
        self.assertEqual(review["classification"], "DATA_QUALITY_ISSUE")
        self.assertTrue(review["is_data_quality_flagged"])

    def test_counterfactual_math_and_summary(self):
        """Counterfactual calculations evaluate alternative SL, targets, and time stop."""
        sig_id = self._insert_test_signal(
            status="SL_HIT",
            exit_reason="SL_HIT",
            entry_price=100.0,
            exit_price=85.0,
            stop_loss=85.0,
            highest_price=108.0,
            lowest_price=84.0,
            duration_mins=18.0,
        )
        review = review_closed_trade(sig_id)
        self.assertIsNotNone(review["counterfactual_results_json"])
        cf = json.loads(review["counterfactual_results_json"])
        self.assertIn("SL_10_PCT", cf)
        self.assertIn("SL_20_PCT", cf)
        self.assertIn("TIME_STOP_45M", cf)
        self.assertIn("ENTRY_AT_LTP", cf)
        # Summary is non-empty and formatted
        self.assertIsNotNone(review["plain_language_summary"])
        self.assertLessEqual(len(review["plain_language_summary"].split("\n")), 5)

    def test_hypothesis_promotion_thresholds(self):
        """Hypotheses require >= 30 observations, positive effect size, and walk-forward confirmation."""
        # 1. Single review creates OBSERVATION
        review_record = {
            "id": 1,
            "signal_id": 101,
            "classification": "STOPPED_BY_NOISE",
            "dte": 1.0,
            "iv": 18.0,
            "r_multiple": -1.0,
            "is_data_quality_flagged": False,
            "created_at": "2026-10-05 11:00:00",
        }
        update_hypotheses_from_review(review_record)

        with _get_db() as conn:
            h = conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id = 'HYP_EXPIRY_TIGHT_SL'").fetchone()
            self.assertIsNotNone(h)
            self.assertEqual(h["status"], "OBSERVATION")
            self.assertEqual(h["observation_count"], 1)

        # 2. Simulate 30 observations supporting noise stop
        with _get_db() as conn:
            conn.execute("UPDATE hypotheses SET observation_count = 29 WHERE hypothesis_id = 'HYP_EXPIRY_TIGHT_SL'")

        review_record_30 = {
            "id": 30,
            "signal_id": 130,
            "classification": "STOPPED_BY_NOISE",
            "dte": 0.5,
            "iv": 18.0,
            "r_multiple": -1.0,
            "is_data_quality_flagged": False,
            "created_at": "2026-10-05 11:30:00",
        }
        update_hypotheses_from_review(review_record_30)

        with _get_db() as conn:
            h_updated = conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id = 'HYP_EXPIRY_TIGHT_SL'").fetchone()
            self.assertGreaterEqual(h_updated["observation_count"], 30)
            self.assertIn(h_updated["status"], ("CANDIDATE", "RECOMMENDED", "REJECTED"))

    def test_data_quality_issue_excluded_from_hypotheses(self):
        """Trades with data quality issues are not added to hypothesis counts."""
        review_bad = {
            "id": 99,
            "signal_id": 999,
            "classification": "DATA_QUALITY_ISSUE",
            "dte": 1.0,
            "iv": 0.0,
            "r_multiple": -1.0,
            "is_data_quality_flagged": True,
            "created_at": "2026-10-05 12:00:00",
        }
        update_hypotheses_from_review(review_bad)

        with _get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM hypotheses WHERE observation_count > 0").fetchone()[0]
            self.assertEqual(count, 0)

    def test_startup_review_unreviewed_closed_trades(self):
        """Startup job automatically finds and reviews closed trades with no existing review."""
        sig1 = self._insert_test_signal(symbol="NIFTY", status="SL_HIT", exit_reason="SL_HIT", exit_price=85.0, highest_price=101.0, lowest_price=84.0)
        sig2 = self._insert_test_signal(symbol="BANKNIFTY", status="CLOSED", exit_reason="TARGET_1", exit_price=130.0, highest_price=132.0, lowest_price=98.0)
        sig_active = self._insert_test_signal(symbol="FINNIFTY", status="ACTIVE")

        reviewed_count = review_unreviewed_closed_trades()
        self.assertEqual(reviewed_count, 2)

        with _get_db() as conn:
            reviews = conn.execute("SELECT signal_id FROM trade_reviews").fetchall()
            reviewed_ids = {r["signal_id"] for r in reviews}
            self.assertIn(sig1, reviewed_ids)
            self.assertIn(sig2, reviewed_ids)
            self.assertNotIn(sig_active, reviewed_ids)


if __name__ == "__main__":
    unittest.main()
