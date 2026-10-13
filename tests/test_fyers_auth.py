"""
tests/test_fyers_auth.py — Unit Tests for Fyers Auth Resiliency & Rate-Limit Protection
"""

import os
import json
import time
import base64
import pytest
from unittest.mock import patch, MagicMock

from app.services.fyers_auth import (
    is_jwt_valid,
    get_cached_token,
    _get_persisted_cooldown,
    _set_persisted_cooldown,
    _clear_persisted_cooldown,
    generate_fyers_access_token,
    TOKEN_CACHE_FILE,
    AUTH_COOLDOWN_FILE,
)


def _make_dummy_jwt(exp_timestamp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode("utf-8").rstrip("=")
    payload_dict = {"fy_id": "XC04484", "exp": int(exp_timestamp)}
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode("utf-8")).decode("utf-8").rstrip("=")
    sig = "dummy_signature"
    return f"{header}.{payload}.{sig}"


def test_is_jwt_valid_future():
    """Verifies that an unexpired token with future exp is valid."""
    future_exp = time.time() + 3600  # 1 hour in future
    token = _make_dummy_jwt(future_exp)
    assert is_jwt_valid(token, buffer_seconds=300) is True


def test_is_jwt_valid_expired():
    """Verifies that a token past its exp is marked invalid."""
    past_exp = time.time() - 100
    token = _make_dummy_jwt(past_exp)
    assert is_jwt_valid(token, buffer_seconds=300) is False


def test_is_jwt_valid_near_expiry():
    """Verifies that a token expiring within buffer seconds is marked invalid."""
    near_exp = time.time() + 100  # within 300s buffer
    token = _make_dummy_jwt(near_exp)
    assert is_jwt_valid(token, buffer_seconds=300) is False


def test_is_jwt_valid_malformed():
    """Verifies handling of non-JWT string."""
    assert is_jwt_valid("invalid_token") is False
    assert is_jwt_valid("") is False
    assert is_jwt_valid(None) is False


def test_persisted_cooldown_lifecycle(tmp_path):
    """Verifies persisting, reading, and clearing cooldown across processes."""
    with patch("app.services.fyers_auth.AUTH_COOLDOWN_FILE", str(tmp_path / "cooldown.json")), \
         patch("app.services.fyers_auth.TOKEN_CACHE_DIR", str(tmp_path)):
        _set_persisted_cooldown(180, "429 Too Many Requests")
        until, err = _get_persisted_cooldown()
        assert until > time.time()
        assert "429" in err

        _clear_persisted_cooldown()
        until2, err2 = _get_persisted_cooldown()
        assert until2 == 0.0
        assert err2 is None


def test_cooldown_prevents_api_calls(tmp_path):
    """Verifies that active cooldown aborts immediately without network calls."""
    with patch("app.services.fyers_auth.AUTH_COOLDOWN_FILE", str(tmp_path / "cooldown.json")), \
         patch("app.services.fyers_auth.TOKEN_CACHE_DIR", str(tmp_path)), \
         patch("app.services.fyers_auth.TOKEN_CACHE_FILE", str(tmp_path / "fyers_token.json")), \
         patch("requests.Session") as mock_session:
        _set_persisted_cooldown(180, "Step 1 send_login_otp failed (429)")

        token, err = generate_fyers_access_token()
        assert token is None
        assert "cooldown" in err.lower()
        mock_session.assert_not_called()
