"""
tests/test_audit_fixes_p0_p1.py
Comprehensive pytest test suite validating fixes for P0 and P1 audit items:
- Item 1: Lot sizes circular truth, validation, and signal-level lot_size cost/R accounting.
- Item 2: Scheduler exclusive lock with dual-process exclusion.
- Item 4: Telemetry archive-to-compressed-file (.csv.gz) before deletion and >= 400 day retention.
- Item 5(d): Calibration test: replaying baseline params on ticks reproduces stored R within tolerance.
- Item 6: Sign-adjusted PE forward moves, day-level clustering, FDR correction, and N_eff.
- Item 7: Per-exchange charges (NSE vs BSE Sensex), GST, STT, and configurable brokerage.
- Item 10: Pure analyze_option_desk without state mutation during UI calls.
"""

import os
import sys
import json
import gzip
import sqlite3
import multiprocessing
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import options_signal_service
from app.services.options_signal_service import (
    _get_db,
    init_signal_db,
    record_signal,
    log_decision,
    cleanup_old_telemetry,
    acquire_scheduler_exclusive_lock,
    release_scheduler_exclusive_lock,
    SIGNAL_EXEC_CONFIG,
)
from app.services.fyers_options_service import INDEX_CONFIGS
from app.services.options_engine import (
    analyze_option_desk,
    ENGINE_CONFIG,
    _BIAS_STATES,
)
from app.services.options_replay_backtest import (
    replay_trade_on_ticks,
    calculate_trade_costs,
    CHARGES_CONFIG,
    DEFAULT_EXIT_CONFIG,
)
from app.services.trade_analyzer_agent import (
    validate_index_configs_against_circulars,
    _enrich_trade_costs_and_r,
    _analyze_setup_pillars_from_decision_log,
)

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _ensure_db_initialized():
    """Initializes tables in the isolated test database for every test."""
    init_signal_db()
    yield


# ==============================================================================
# Helper Subprocess Worker for Dual-Process Lock Testing (Item 2)
# ==============================================================================

def _lock_holder_child(lock_id, db_dir, db_path, start_event, stop_event, result_queue):
    """Child process that configures test DB path, acquires exclusive lock, waits, and releases."""
    options_signal_service.DB_DIR = db_dir
    options_signal_service.DB_PATH = db_path
    init_signal_db()

    acquired = acquire_scheduler_exclusive_lock(lock_id=lock_id, lease_timeout_sec=15)
    result_queue.put(acquired)
    start_event.set()
    if acquired:
        stop_event.wait(timeout=10)
        release_scheduler_exclusive_lock(lock_id=lock_id)


# ==============================================================================
# Item 1: Lot Sizes & Signal-Level Accounting
# ==============================================================================

def test_item_1_circular_lot_sizes():
    """Validates that INDEX_CONFIGS matches NSE dated circulars for NIFTY."""
    assert INDEX_CONFIGS["NIFTY"]["lot_size"] == 65
    assert validate_index_configs_against_circulars() is True


def test_item_1_costs_and_r_use_signal_own_lot_size():
    """Validates that cost and R calculations use the signal's stored lot_size."""
    # Trade with stored lot_size 65 (NIFTY) vs custom lot_size 100
    trade_65 = {
        "id": 101,
        "symbol": "NIFTY",
        "entry_price": 100.0,
        "exit_price": 120.0,
        "initial_stop_loss": 80.0,
        "stop_loss": 80.0,
        "lots": 2,
        "lot_size": 65,
        "status": "CLOSED",
        "exit_reason": "TARGET_2_HIT",
    }
    trade_100 = {
        "id": 102,
        "symbol": "NIFTY",
        "entry_price": 100.0,
        "exit_price": 120.0,
        "initial_stop_loss": 80.0,
        "stop_loss": 80.0,
        "lots": 2,
        "lot_size": 100,
        "status": "CLOSED",
        "exit_reason": "TARGET_2_HIT",
    }

    enriched_65 = _enrich_trade_costs_and_r(trade_65)
    enriched_100 = _enrich_trade_costs_and_r(trade_100)

    # Net INR must scale with stored lot_size
    assert enriched_65["net_pnl_inr"] == pytest.approx(enriched_65["net_pnl_pts"] * 2 * 65, abs=1.0)
    assert enriched_100["net_pnl_inr"] == pytest.approx(enriched_100["net_pnl_pts"] * 2 * 100, abs=1.0)
    assert enriched_100["net_pnl_inr"] > enriched_65["net_pnl_inr"]


# ==============================================================================
# Item 2: Scheduler Lock with Dual-Process Exclusion
# ==============================================================================

def test_item_2_scheduler_lock_two_processes():
    """Tests that a second concurrent process fails to acquire the exclusive lock."""
    lock_id = "test_sched_lock_p0"
    start_event = multiprocessing.Event()
    stop_event = multiprocessing.Event()
    q = multiprocessing.Queue()

    p = multiprocessing.Process(
        target=_lock_holder_child,
        args=(
            lock_id,
            options_signal_service.DB_DIR,
            options_signal_service.DB_PATH,
            start_event,
            stop_event,
            q,
        ),
    )
    p.start()
    try:
        assert start_event.wait(timeout=5)
        child_acquired = q.get(timeout=2)
        assert child_acquired is True, "Child process should have acquired lock"

        # Now try to acquire from this main process — must FAIL
        main_acquired = acquire_scheduler_exclusive_lock(lock_id=lock_id, lease_timeout_sec=15)
        assert main_acquired is False, "Second process must fail to acquire the lock"
    finally:
        stop_event.set()
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()

    # After child release, main process should succeed
    main_acquired_after = acquire_scheduler_exclusive_lock(lock_id=lock_id, lease_timeout_sec=15)
    assert main_acquired_after is True, "Process should be able to acquire lock after release"
    release_scheduler_exclusive_lock(lock_id=lock_id)


# ==============================================================================
# Item 4: Telemetry Archive Before Deletion and Retention >= 400 Days
# ==============================================================================

def test_item_4_retention_archive_to_compressed_file(tmp_path):
    """
    Tests that cleanup_old_telemetry:
    1. Archives rows to .csv.gz before deletion.
    2. Does not prune rows younger than retention_days (default >= 400).
    3. Never prunes rows referenced by signals, reviews, or completed labels.
    """
    archive_dir = str(tmp_path / "archives")
    os.makedirs(archive_dir, exist_ok=True)

    old_ts = (datetime.now(IST) - timedelta(days=450)).strftime("%Y-%m-%d %H:%M:%S")

    with _get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 1. Old unreferenced tick (eligible for archive & cleanup)
        conn.execute("""
            INSERT INTO trade_ticks (signal_id, timestamp, ltp, bid, ask, spot, created_at)
            VALUES (999991, ?, 100.0, 99.5, 100.5, 24000.0, ?)
        """, (old_ts, old_ts))
        unref_tick_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 2. Old unreferenced decision_log (eligible)
        conn.execute("""
            INSERT INTO decision_log (
                timestamp, symbol, spot_price, spot_change_pct, atm_strike,
                pcr, bullish_score, bearish_score, bias, pillar_direction,
                pillar_level, pillar_momentum, pillar_volume, pillar_oi, pillar_iv_session,
                setup_score, decision, created_at, labels_status
            ) VALUES (?, 'NIFTY', 24000.0, 0.1, 24000.0, 1.0, 50.0, 50.0, 'NEUTRAL', 0, 0, 0, 0, 0, 0, 50, 'WAIT', ?, 'SKIPPED')
        """, (old_ts, old_ts))
        unref_dl_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 3. Old decision_log with COMPLETED labels (must NOT be pruned)
        conn.execute("""
            INSERT INTO decision_log (
                timestamp, symbol, spot_price, spot_change_pct, atm_strike,
                pcr, bullish_score, bearish_score, bias, pillar_direction,
                pillar_level, pillar_momentum, pillar_volume, pillar_oi, pillar_iv_session,
                setup_score, decision, created_at, labels_status
            ) VALUES (?, 'NIFTY', 24000.0, 0.1, 24000.0, 1.0, 50.0, 50.0, 'NEUTRAL', 0, 0, 0, 0, 0, 0, 50, 'WAIT', ?, 'COMPLETED')
        """, (old_ts, old_ts))
        protected_dl_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("COMMIT")

    # Run cleanup with default retention (400 days)
    res = cleanup_old_telemetry(retention_days=400, archive_dir=archive_dir)

    assert res["retention_days"] >= 400
    assert res["ticks_archived"] >= 1
    assert res["decisions_archived"] >= 1
    assert len(res["archive_files"]) >= 2

    # Verify that .csv.gz archive files exist and are valid gzip files
    for fpath in res["archive_files"]:
        assert os.path.exists(fpath)
        with gzip.open(fpath, "rt", encoding="utf-8") as gz:
            lines = gz.readlines()
            assert len(lines) >= 2  # header + at least 1 row

    # Verify DB states: unreferenced deleted, protected preserved
    with _get_db() as conn:
        t_row = conn.execute("SELECT id FROM trade_ticks WHERE id = ?", (unref_tick_id,)).fetchone()
        assert t_row is None, "Unreferenced old tick should be pruned"

        dl_unref = conn.execute("SELECT id FROM decision_log WHERE id = ?", (unref_dl_id,)).fetchone()
        assert dl_unref is None, "Unreferenced old decision should be pruned"

        dl_protected = conn.execute("SELECT id FROM decision_log WHERE id = ?", (protected_dl_id,)).fetchone()
        assert dl_protected is not None, "Completed label decision row must never be pruned"


# ==============================================================================
# Item 5(d): Calibration Test: Replaying Baseline Params on Stored Ticks
# ==============================================================================

def test_item_5d_replay_calibration_matches_stored_r():
    """
    Calibration test (Item 5d):
    Replaying baseline exit parameters on synthetic stored ticks must reproduce stored R within stated tolerance (±0.15 R).
    """
    signal = {
        "id": 888,
        "symbol": "NIFTY",
        "entry_price": 100.0,
        "target_1": 115.0,  # 1.5R with 10pt risk
        "target_2": 125.0,  # 2.5R
        "initial_stop_loss": 90.0,
        "stop_loss": 90.0,
        "lots": 2,
        "lot_size": 65,
    }

    # Simulate ticks rising to hit T1, trailing, and hitting T2
    ticks = [
        {"ltp": 100.0, "bid": 99.5, "ask": 100.5, "spot": 24000.0, "timestamp": "2026-06-01 10:00:00"},
        {"ltp": 105.0, "bid": 104.5, "ask": 105.5, "spot": 24020.0, "timestamp": "2026-06-01 10:01:00"},
        {"ltp": 115.5, "bid": 115.0, "ask": 116.0, "spot": 24060.0, "timestamp": "2026-06-01 10:02:00"}, # T1 Hit
        {"ltp": 120.0, "bid": 119.5, "ask": 120.5, "spot": 24080.0, "timestamp": "2026-06-01 10:03:00"},
        {"ltp": 126.0, "bid": 125.5, "ask": 126.5, "spot": 24100.0, "timestamp": "2026-06-01 10:04:00"}, # T2 Hit
    ]

    replayed_net_r = replay_trade_on_ticks(signal, ticks, params=DEFAULT_EXIT_CONFIG)

    # Compute expected R using _enrich_trade_costs_and_r
    synthetic_closed_trade = {
        "id": 888,
        "symbol": "NIFTY",
        "entry_price": 100.0,
        "target_1": 115.0,
        "exit_price": 125.5,
        "initial_stop_loss": 90.0,
        "stop_loss": 90.0,
        "lots": 2,
        "lot_size": 65,
        "status": "TARGET_1_HIT",
        "exit_reason": "TARGET_2_HIT",
    }
    enriched = _enrich_trade_costs_and_r(synthetic_closed_trade)
    expected_r = enriched["r_multiple"]

    # Calibration tolerance: ±0.15 R
    assert abs(replayed_net_r - expected_r) < 0.15, (
        f"Calibration mismatch: replayed {replayed_net_r} vs expected {expected_r}"
    )


# ==============================================================================
# Item 6: Decision Log Pillar Analysis
# ==============================================================================

def test_item_6_pillar_analysis_sign_adjustment_and_day_clustering():
    """
    Tests that _analyze_setup_pillars_from_decision_log:
    1. Sign-adjusts PE moves (multiplies by -1.0 so downwards move is positive gain).
    2. Evaluates only cycles where candidate side is CE or PE.
    3. Clusters by trading day and applies FDR across all pillars.
    4. Reports effective sample size N_eff.
    """
    # Insert 32 rows across two days into decision_log to cross the 30-row evaluation threshold
    with _get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM decision_log WHERE symbol = 'TEST_SYM'")

        # Day 1: CE candidate, spot moved up (+1.5 ATR) -> positive forward move
        for i in range(16):
            ts = f"2026-06-01 10:{i:02d}:00"
            conn.execute("""
                INSERT INTO decision_log (
                    timestamp, symbol, spot_price, spot_change_pct, atm_strike,
                    pcr, bullish_score, bearish_score, bias, pillar_direction,
                    pillar_level, pillar_momentum, pillar_volume, pillar_oi, pillar_iv_session,
                    setup_score, candidate_side, decision, forward_underlying_30m,
                    labels_status, created_at
                ) VALUES (?, 'TEST_SYM', 24000.0, 0.1, 24000.0, 1.0, 60.0, 40.0, 'BULLISH', 1, 1, 1, 1, 1, 1, 65, 'CE', 'BUY CE', 1.5, 'COMPLETED', ?)
            """, (ts, ts))

        # Day 2: PE candidate, spot moved DOWN (-1.2 ATR) -> under sign-adjustment, forward move is +1.2
        for i in range(16):
            ts = f"2026-06-02 10:{i:02d}:00"
            conn.execute("""
                INSERT INTO decision_log (
                    timestamp, symbol, spot_price, spot_change_pct, atm_strike,
                    pcr, bullish_score, bearish_score, bias, pillar_direction,
                    pillar_level, pillar_momentum, pillar_volume, pillar_oi, pillar_iv_session,
                    setup_score, candidate_side, decision, forward_underlying_30m,
                    labels_status, created_at
                ) VALUES (?, 'TEST_SYM', 24000.0, -0.1, 24000.0, 0.8, 40.0, 60.0, 'BEARISH', 1, 1, 1, 1, 1, 1, 65, 'PE', 'BUY PE', -1.2, 'COMPLETED', ?)
            """, (ts, ts))
        conn.execute("COMMIT")

    try:
        results = _analyze_setup_pillars_from_decision_log()
        assert len(results) == 6

        p_dir = next(p for p in results if p["pillar_id"] == "direction")
        assert p_dir["sign_adjusted_for_side"] is True
        # Under sign adjustment, the pass mean combines +1.5 for CE and +1.2 for PE -> both positive!
        assert p_dir["pass_stats"]["mean_forward_atr_30m"] > 1.0
        assert p_dir["effective_sample_size"] > 0
        assert p_dir["trading_days_evaluated"] >= 2
        assert "fdr_p_value" in p_dir
    finally:
        with _get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM decision_log WHERE symbol = 'TEST_SYM'")
            conn.execute("COMMIT")


# ==============================================================================
# Item 7: Per-Exchange Charges Config & Configurable Brokerage
# ==============================================================================

def test_item_7_charges_config_per_exchange_and_brokerage():
    """Validates per-exchange rates (NSE 0.035%, BSE Sensex 0.0325%, STT 0.15%, Stamp 0.003%, GST 18%)."""
    # 1. NSE charges test
    costs_nse = calculate_trade_costs(
        entry_price=100.0,
        exit_prices=[(120.0, 1.0)],
        lot_size=65,
        lots=1,
        symbol="NIFTY",
        brokerage_per_order=0.0, # Zero brokerage test
    )
    assert costs_nse["brokerage"] == 0.0
    assert costs_nse["exchange_charges"] > 0.0
    # Buy turnover = 6500, Sell turnover = 7800. Exch = 0.035% of total
    expected_exch = (6500 + 7800) * 0.00035
    assert costs_nse["exchange_charges"] == pytest.approx(expected_exch, abs=0.1)

    # 2. BSE Sensex charges test (0.0325% rate)
    costs_bse = calculate_trade_costs(
        entry_price=100.0,
        exit_prices=[(120.0, 1.0)],
        lot_size=20,
        lots=1,
        symbol="SENSEX",
        brokerage_per_order=20.0, # 20 per order
    )
    assert costs_bse["brokerage"] == 40.0 # 2 orders * 20
    expected_exch_bse = (2000 + 2400) * 0.000325
    assert costs_bse["exchange_charges"] == pytest.approx(expected_exch_bse, abs=0.1)

    # GST is 18% on (Brokerage + Exchange + SEBI)
    expected_gst = (costs_bse["brokerage"] + costs_bse["exchange_charges"] + costs_bse["sebi_charges"]) * 0.18
    assert costs_bse["gst"] == pytest.approx(expected_gst, abs=0.1)


# ==============================================================================
# Item 10: Pure analyze_option_desk without State Mutation
# ==============================================================================

def test_item_10_analyze_option_desk_pure_function():
    """
    Validates that analyze_option_desk:
    - Does NOT mutate _BIAS_STATES when update_state=False (UI calls).
    - Returns deterministic results without side effects.
    """
    mock_data = {
        "symbol": "NIFTY",
        "spot_price": 24000.0,
        "spot_change_pct": 0.5,
        "chain": [
            {"strike": 24000.0, "ce_ltp": 120.0, "pe_ltp": 110.0, "ce_bid": 119.5, "ce_ask": 120.5, "pe_bid": 109.5, "pe_ask": 110.5, "ce_oi": 10000, "pe_oi": 15000, "ce_volume": 5000, "pe_volume": 8000, "ce_iv": 14.5, "pe_iv": 15.0},
        ],
        "vix": 13.2,
        "dte": 3.0,
    }

    # Record initial state
    init_state = dict(_BIAS_STATES.get("NIFTY", {}))

    # Call analyze_option_desk 3 times with update_state=False
    for _ in range(3):
        res = analyze_option_desk(mock_data, update_state=False)
        assert "option_buying" in res
        assert "market_bias" in res

    # Verify that global _BIAS_STATES was untouched
    post_state = dict(_BIAS_STATES.get("NIFTY", {}))
    assert init_state == post_state, "analyze_option_desk with update_state=False must not mutate _BIAS_STATES"
