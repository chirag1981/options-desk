"""
tests/test_api.py — Integration Tests for Flask REST Endpoints & Dashboard Routes
"""

import pytest
from app import create_app
from app.config import Config
from app.extensions import db


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


@pytest.fixture
def client():
    app = create_app(TestConfig)
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            yield client
            db.session.remove()
            db.drop_all()


def test_dashboard_routes(client):
    r1 = client.get("/")
    assert r1.status_code == 200
    assert b"NIFTY Options Selling Desk" in r1.data

    r2 = client.get("/journal")
    assert r2.status_code == 200

    r3 = client.get("/analysis")
    assert r3.status_code == 200

    r4 = client.get("/diagnostics")
    assert r4.status_code == 200

    r5 = client.get("/settings")
    assert r5.status_code == 200


def test_api_market_data(client):
    res = client.get("/api/market-data")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "success"
    assert "spot_price" in data["data"]
    assert "pcr_oi" in data["data"]


def test_api_candidates(client):
    res = client.get("/api/candidates")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "success"
    assert "intraday" in data
    assert "positional" in data
    assert "previews" in data


def test_api_positions_and_export_csv(client):
    r1 = client.get("/api/positions")
    assert r1.status_code == 200
    data = r1.get_json()
    assert data["status"] == "success"
    assert "summary" in data

    r2 = client.get("/api/export-csv")
    assert r2.status_code == 200
    assert r2.mimetype == "text/csv"
    assert b"Trade ID,Engine,Strategy" in r2.data


def test_api_no_cache_headers(client):
    """Verifies that API polling endpoints return no-store/no-cache headers to prevent stale browser data."""
    res = client.get("/api/candidates")
    assert res.status_code == 200
    assert "no-store" in res.headers.get("Cache-Control", "")
    assert "no-cache" in res.headers.get("Cache-Control", "")
    assert res.headers.get("Pragma") == "no-cache"


def test_fyers_endpoints(client):
    """Verifies FYERS status check and token updating endpoints."""
    # Status endpoint
    r1 = client.get("/api/fyers/status")
    assert r1.status_code == 200
    d1 = r1.get_json()
    assert d1["status"] == "success"
    assert "is_live" in d1

    # Empty token set rejection
    r2 = client.post("/api/fyers/set-token", json={"access_token": ""})
    assert r2.status_code == 400


def test_fyers_initialize_connection(monkeypatch):
    """Verifies MarketDataProvider.initialize_connection behavior on app startup."""
    from app.market_data import MarketDataProvider

    # Mock get_access_token returning None
    monkeypatch.setattr(MarketDataProvider, "get_access_token", classmethod(lambda cls: None))
    conn, status, spot = MarketDataProvider.initialize_connection()
    assert conn is False
    assert status == "DISCONNECTED"

    # Mock get_access_token returning active token and get_spot_price returning LIVE
    monkeypatch.setattr(MarketDataProvider, "get_access_token", classmethod(lambda cls: "mock_tok"))
    monkeypatch.setattr(MarketDataProvider, "get_spot_price", classmethod(lambda cls: (22500.0, 0.4, "FYERS_LIVE")))
    conn2, status2, spot2 = MarketDataProvider.initialize_connection()
    assert conn2 is True
    assert status2 == "FYERS_LIVE"
    assert spot2 == 22500.0


