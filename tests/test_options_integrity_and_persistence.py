"""
tests/test_options_integrity_and_persistence.py - Verification tests for:
1. Market Data Integrity & DATA_UNAVAILABLE fail-closed strategy decisions.
2. Final CE/PE Entry Persistence (consecutive cycle confirmations required before recording signal).
3. Telemetry recording of candidate side, confirmation counts, and required confirmations.
"""

import unittest
import json
from datetime import datetime
from app.services.options_engine import analyze_option_desk, ENGINE_CONFIG
from app.services.fyers_options_service import fetch_option_chain_data
from app.services.options_scheduler import (
    update_pending_entry_confirmation,
    reset_pending_entry_confirmation,
    get_pending_entry_confirmation,
)
from app.services.options_signal_service import (
    _get_db,
    log_decision,
    init_signal_db,
)


class TestOptionsIntegrityAndPersistence(unittest.TestCase):

    def setUp(self):
        init_signal_db()
        reset_pending_entry_confirmation("NIFTY")
        reset_pending_entry_confirmation("BANKNIFTY")

    def test_data_unavailable_fails_closed(self):
        """1. When data_status is DATA_UNAVAILABLE or chain is empty, engine must fail closed and return WAIT."""
        market_data = {
            "symbol": "NIFTY",
            "spot_price": 22500.0,
            "strike_step": 50,
            "expiry": "06-OCT-2026",
            "data_status": "DATA_UNAVAILABLE",
            "chain": [],
        }

        res = analyze_option_desk(market_data)
        self.assertEqual(res.get("option_buying", {}).get("decision"), "WAIT")
        self.assertFalse(res.get("option_buying", {}).get("data_quality", {}).get("valid", True))
        self.assertIn("unavailable", res.get("option_buying", {}).get("reason", "").lower())

    def test_simulated_data_never_triggers_live_decision(self):
        """2. Synthetic data (marked SIMULATED) must never pass LIVE gate or trigger BUY decision in live mode."""
        chain = [{
            "strike": 22500,
            "ce_oi": 500000,
            "ce_change_oi": -200000,
            "ce_volume": 1000000,
            "ce_ltp": 120.0,
            "ce_bid": 119.5,
            "ce_ask": 120.5,
            "ce_iv": 14.5,
            "pe_oi": 800000,
            "pe_change_oi": 300000,
            "pe_volume": 1200000,
            "pe_ltp": 80.0,
            "pe_bid": 79.5,
            "pe_ask": 80.5,
            "pe_iv": 14.5,
        }]

        market_data = {
            "symbol": "NIFTY",
            "spot_price": 22550.0,
            "strike_step": 50,
            "expiry": "06-OCT-2026",
            "data_status": "SIMULATED",  # Not LIVE!
            "chain": chain,
        }

        res = analyze_option_desk(market_data)
        # Even if setup score is high, it MUST return WAIT because data_status != LIVE
        self.assertEqual(res.get("option_buying", {}).get("decision"), "WAIT")
        self.assertIn("LIVE market data required", res.get("option_buying", {}).get("reason", ""))

    def test_consecutive_entry_confirmation_flow(self):
        """3. Entry persistence requires CONSECUTIVE_CONFIRMATIONS_REQUIRED consecutive cycles before BUY."""
        symbol = "NIFTY"
        req_confs = 2

        # Cycle 1: Candidate CE passes gates -> confirmation 1 -> WAIT
        count1, is_confirmed1 = update_pending_entry_confirmation(symbol, "CE", req_confs=req_confs)
        self.assertEqual(count1, 1)
        self.assertFalse(is_confirmed1)

        # Cycle 2: Candidate CE passes gates again -> confirmation 2 -> CONFIRMED BUY
        count2, is_confirmed2 = update_pending_entry_confirmation(symbol, "CE", req_confs=req_confs)
        self.assertEqual(count2, 2)
        self.assertTrue(is_confirmed2)

        # Reset on signal entry
        reset_pending_entry_confirmation(symbol)
        state = get_pending_entry_confirmation(symbol)
        self.assertEqual(state["confirmation_count"], 0)
        self.assertIsNone(state["candidate_side"])

    def test_confirmation_reset_on_failure_or_flip(self):
        """4. If any gate fails (WAIT) or opposite side appears (PE after CE), confirmation resets."""
        symbol = "BANKNIFTY"
        req_confs = 2

        # Cycle 1: CE passes -> count = 1
        count1, is_conf1 = update_pending_entry_confirmation(symbol, "CE", req_confs=req_confs)
        self.assertEqual(count1, 1)
        self.assertFalse(is_conf1)

        # Cycle 2: Gate fails -> None / WAIT -> count resets to 0
        count2, is_conf2 = update_pending_entry_confirmation(symbol, None, req_confs=req_confs)
        self.assertEqual(count2, 0)
        self.assertFalse(is_conf2)

        # Cycle 3: CE passes again -> starts fresh count = 1
        count3, is_conf3 = update_pending_entry_confirmation(symbol, "CE", req_confs=req_confs)
        self.assertEqual(count3, 1)
        self.assertFalse(is_conf3)

        # Cycle 4: Opposite side appears (PE) -> resets CE, starts PE count = 1
        count4, is_conf4 = update_pending_entry_confirmation(symbol, "PE", req_confs=req_confs)
        self.assertEqual(count4, 1)
        self.assertFalse(is_conf4)

    def test_telemetry_confirmation_logging_in_decision_log(self):
        """5. Telemetry must store candidate_side, confirmation_count, required_confirmations in decision_log and raw_values_json."""
        payload = {
            "symbol": "FINNIFTY",
            "spot_price": 24500.0,
            "spot_change_pct": 0.45,
            "atm_strike": 24500.0,
            "atm_iv": 14.5,
            "pcr": 1.25,
            "bullish_score": 80.0,
            "bearish_score": 20.0,
            "bias": "BULLISH",
            "pillar_flags": {"direction": 1, "level": 1, "momentum": 1, "volume": 1, "oi": 1, "iv_session": 1},
            "raw_values": {"spot": 24500.0, "pcr": 1.25},
            "data_quality": {"valid": True},
            "setup_score": 85,
            "candidate_side": "CE",
            "confirmation_count": 1,
            "required_confirmations": 2,
            "decision": "WAIT",
            "decision_reason": "Candidate CE BUY pending confirmation (1/2 evaluations).",
        }

        row_id = log_decision(payload)
        self.assertIsNotNone(row_id)

        with _get_db() as conn:
            row = conn.execute("SELECT * FROM decision_log WHERE id = ?", (row_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["candidate_side"], "CE")
            self.assertEqual(row["confirmation_count"], 1)
            self.assertEqual(row["required_confirmations"], 2)
            self.assertEqual(row["decision"], "WAIT")

            # Check raw_values_json contains the confirmation fields
            raw_vals = json.loads(row["raw_values_json"] or "{}")
            self.assertEqual(raw_vals.get("candidate_side"), "CE")
            self.assertEqual(raw_vals.get("confirmation_count"), 1)
            self.assertEqual(raw_vals.get("required_confirmations"), 2)


if __name__ == "__main__":
    unittest.main()
