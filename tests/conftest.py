"""
tests/conftest.py — Pytest Configuration & Path Injection
Ensures workspace root is on sys.path for test discovery.
"""

import sys
import os

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)
