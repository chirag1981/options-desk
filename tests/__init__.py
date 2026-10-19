"""
tests/__init__.py — Package initializer for Options Desk tests.
Guarantees database isolation when running under unittest or IDE test runners,
redirecting options_signal_service.DB_PATH to a temporary SQLite database
so tests never touch or truncate instance/options_signals.db.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import options_signal_service

if "instance" in getattr(options_signal_service, "DB_PATH", ""):
    _TEST_DB_DIR = tempfile.mkdtemp(prefix="options_desk_test_pkg_")
    options_signal_service.DB_DIR = _TEST_DB_DIR
    options_signal_service.DB_PATH = os.path.join(_TEST_DB_DIR, "options_signals_test.db")
    options_signal_service.init_signal_db()
