"""
tests/conftest.py — Isolates the test suite from the live trade journal.
Redirects options_signal_service.DB_PATH to a throwaway SQLite file so tests
never insert fixture trades into (or DELETE from) instance/options_signals.db.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.services import options_signal_service

_TEST_DB_DIR = tempfile.mkdtemp(prefix="options_desk_test_")
options_signal_service.DB_DIR = _TEST_DB_DIR
options_signal_service.DB_PATH = os.path.join(_TEST_DB_DIR, "options_signals_test.db")


@pytest.fixture(autouse=True)
def _assert_isolated_db():
    """Fails fast if any test is about to touch the live database."""
    assert "instance" not in options_signal_service.DB_PATH, "Tests must not use the live DB"
    yield


@pytest.fixture(autouse=True)
def _pin_engine_clock(monkeypatch):
    """Pins the engine's session/IV filter to 11:00 IST so gate tests don't depend on wall-clock time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services import options_engine

    original = options_engine.evaluate_iv_and_time_filter
    fixed_now = datetime(2026, 10, 5, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr(
        options_engine,
        "evaluate_iv_and_time_filter",
        lambda atm_iv, now_dt=None: original(atm_iv, now_dt or fixed_now),
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_oi_baseline(monkeypatch, tmp_path):
    """Keeps the per-day OI baseline out of instance/ during tests."""
    from app.services import fyers_options_service as fos

    monkeypatch.setattr(fos, "_OI_BASELINE_PATH", str(tmp_path / "oi_baseline.json"))
    monkeypatch.setattr(fos, "_OI_BASELINE", {"date": None, "oi": {}})
    yield
