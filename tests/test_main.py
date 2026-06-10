import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from ditare_api.main import app, settings, redis_client

client = TestClient(app)

# Existing tests...

def test_revenuecat_webhook_unauthorized():
    """Should reject requests with missing or invalid signature."""
    with patch.object(settings, "revenuecat_webhook_secret", "test-secret"):
        # Missing auth header
        resp = client.post("/webhooks/revenuecat", json={"event": {"id": "evt-1", "type": "INITIAL_PURCHASE", "app_user_id": "u1"}})
        assert resp.status_code == 401
        
        # Wrong auth header
        resp = client.post("/webhooks/revenuecat", json={"event": {"id": "evt-1", "type": "INITIAL_PURCHASE", "app_user_id": "u1"}},
                          headers={"Authorization": "Bearer wrong-secret"})
        assert resp.status_code == 401

def test_revenuecat_webhook_missing_fields():
    """Should reject events missing id or app_user_id."""
    with patch.object(settings, "revenuecat_webhook_secret", "test-secret"):
        # Missing event.id
        resp = client.post("/webhooks/revenuecat", json={"event": {"type": "INITIAL_PURCHASE", "app_user_id": "u1"}},
                          headers={"Authorization": "Bearer test-secret"})
        assert resp.status_code == 400
        
        # Missing app_user_id
        resp = client.post("/webhooks/revenuecat", json={"event": {"id": "evt-1", "type": "INITIAL_PURCHASE"}},
                          headers={"Authorization": "Bearer test-secret"})
        assert resp.status_code == 400

def test_revenuecat_webhook_idempotency():
    """Should deduplicate by event.id using Redis."""
    with patch.object(settings, "revenuecat_webhook_secret", "test-secret"):
        # Clear any stale data
        redis_client.delete("revenuecat:processed:evt-dup")
        redis_client.delete("entitlement:pro:user-dup")
        
        payload = {"event": {"id": "evt-dup", "type": "INITIAL_PURCHASE", "app_user_id": "user-dup"}}
        headers = {"Authorization": "Bearer test-secret"}
        
        # First call should process
        resp1 = client.post("/webhooks/revenuecat", json=payload, headers=headers)
        assert resp1.status_code == 200
        assert resp1.json()["ok"] is True
        
        # Second call should be idempotent
        resp2 = client.post("/webhooks/revenuecat", json=payload, headers=headers)
        assert resp2.status_code == 200
        assert resp2.json().get("idempotent") is True
        
        # Cleanup
        redis_client.delete("revenuecat:processed:evt-dup")
        redis_client.delete("entitlement:pro:user-dup")

def test_revenuecat_webhook_sets_active():
    """Should set entitlement active for purchase events."""
    with patch.object(settings, "revenuecat_webhook_secret", "test-secret"):
        redis_client.delete("revenuecat:processed:evt-act")
        redis_client.delete("entitlement:pro:user-act")
        
        payload = {"event": {"id": "evt-act", "type": "INITIAL_PURCHASE", "app_user_id": "user-act"}}
        resp = client.post("/webhooks/revenuecat", json=payload, headers={"Authorization": "Bearer test-secret"})
        
        assert resp.status_code == 200
        assert redis_client.get("entitlement:pro:user-act") == "active"
        
        redis_client.delete("revenuecat:processed:evt-act")
        redis_client.delete("entitlement:pro:user-act")

def test_revenuecat_webhook_sets_inactive():
    """Should set entitlement inactive for cancellation events."""
    with patch.object(settings, "revenuecat_webhook_secret", "test-secret"):
        redis_client.delete("revenuecat:processed:evt-deact")
        redis_client.delete("entitlement:pro:user-deact")
        
        payload = {"event": {"id": "evt-deact", "type": "CANCELLATION", "app_user_id": "user-deact"}}
        resp = client.post("/webhooks/revenuecat", json=payload, headers={"Authorization": "Bearer test-secret"})
        
        assert resp.status_code == 200
        assert redis_client.get("entitlement:pro:user-deact") == "inactive"
        
        redis_client.delete("revenuecat:processed:evt-deact")
        redis_client.delete("entitlement:pro:user-deact")

# Existing tests from test_main.py
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
    resp = client.post("/transcribe")
    assert resp.status_code in [403, 422]

def test_cleanup_no_auth():
    resp = client.post("/cleanup", json={"text": "hello"})
    assert resp.status_code in [403, 422]

def test_auth_exchange_not_configured():
    with patch.object(settings, "apple_team_id", ""), patch.object(settings, "apple_bundle_id", ""):
        resp = client.post("/auth/exchange", json={"identity_token": "test", "apple_user_id": "test"})
        assert resp.status_code == 501

def test_revenuecat_webhook_not_configured():
    resp = client.post("/webhooks/revenuecat", json={"event": {"type": "TEST"}})
    assert resp.status_code == 501