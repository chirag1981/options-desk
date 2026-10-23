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

