import pytest
from fastapi.testclient import TestClient
from ditare_api.main import app

client = TestClient(app)

def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["region"] == "gru"

def test_root():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Ditare API" in resp.json()["service"]

def test_me_no_auth():
    resp = client.get("/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["entitlement"] == "free"
    assert data["user_id"] is None

def test_transcribe_no_auth():
    # Should fail without auth token
    resp = client.post("/transcribe")
    assert resp.status_code in [403, 422]

def test_cleanup_no_auth():
    resp = client.post("/cleanup", json={"text": "hello"})
    assert resp.status_code in [403, 422]

from unittest.mock import patch
from ditare_api.main import settings

def test_auth_exchange_not_configured():
    with patch.object(settings, "apple_team_id", ""), patch.object(settings, "apple_bundle_id", ""):
        resp = client.post("/auth/exchange", json={"identity_token": "test", "apple_user_id": "test"})
        assert resp.status_code == 501

def test_revenuecat_webhook_not_configured():
    resp = client.post("/webhooks/revenuecat", json={"event": {"type": "TEST"}})
    assert resp.status_code == 501
