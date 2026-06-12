import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, AsyncMock
from ditare_api.main import app, settings, redis_client

client = TestClient(app)

# CORS tests
def test_cors_allowed_origin():
    """Should allow requests from allowed origins."""
    resp = client.get("/", headers={"Origin": "https://ditare.app"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers
    assert resp.headers["access-control-allow-origin"] == "https://ditare.app"

def test_cors_blocked_origin():
    """Should not allow requests from disallowed origins."""
    resp = client.get("/", headers={"Origin": "https://evil.com"})
    assert resp.status_code == 200
    # When origin is not allowed, CORS middleware does not set the header
    # or sets it to the allowed origin (depending on implementation)
    # The key is that the browser would block it
    assert resp.headers.get("access-control-allow-origin") != "https://evil.com"

def test_cors_preflight_allowed():
    """Should allow preflight from allowed origin."""
    resp = client.options(
        "/transcribe",
        headers={
            "Origin": "https://ditare.vercel.app",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization",
        }
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://ditare.vercel.app"

def test_cors_preflight_blocked():
    """Should block preflight from disallowed origin."""
    resp = client.options(
        "/transcribe",
        headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "POST",
        }
    )
    assert resp.status_code == 400  # CORS middleware rejects preflight for disallowed origin

# API key missing tests
def test_transcribe_missing_api_key_returns_403():
    """Should return 403 (not 500) when Groq API key is missing."""
    with patch.object(settings, "env", "development"), patch.object(settings, "groq_api_key", ""):
        # In development, entitlement check passes with any token
        resp = client.post(
            "/transcribe",
            files={"audio": ("test.m4a", b"fake audio", "audio/m4a")},
            headers={"Authorization": "Bearer fake-token"}
        )
        assert resp.status_code == 403
        assert "Groq API key not configured" in resp.json()["detail"]

def test_cleanup_missing_api_key_returns_403():
    """Should return 403 (not 500) when OpenAI API key is missing."""
    with patch.object(settings, "env", "development"), patch.object(settings, "openai_api_key", ""):
        # In development, entitlement check passes with any token
        resp = client.post(
            "/cleanup",
            json={"text": "hello"},
            headers={"Authorization": "Bearer fake-token"}
        )
        assert resp.status_code == 403
        assert "OpenAI API key not configured" in resp.json()["detail"]

# Entitlement cache tests
@pytest.mark.asyncio
async def test_entitlement_cache_hit():
    """Should use cached entitlement from Redis."""
    from ditare_api.main import check_pro_entitlement
    
    with patch.object(settings, "env", "production"):
        # Set up cache
        redis_client.setex("entitlement:pro:cache:test-user", 300, "active")
        
        with patch("ditare_api.main.get_user_id", return_value="test-user"):
            result = await check_pro_entitlement("some-token")
            assert result is True
        
        redis_client.delete("entitlement:pro:cache:test-user")

@pytest.mark.asyncio
async def test_entitlement_cache_inactive():
    """Should return False for cached inactive entitlement."""
    from ditare_api.main import check_pro_entitlement
    
    with patch.object(settings, "env", "production"):
        redis_client.setex("entitlement:pro:cache:test-user-inactive", 300, "inactive")
        
        with patch("ditare_api.main.get_user_id", return_value="test-user-inactive"):
            result = await check_pro_entitlement("some-token")
            assert result is False
        
        redis_client.delete("entitlement:pro:cache:test-user-inactive")

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
# RevenueCat REST API entitlement tests (review follow-ups from CHRA-2504)

def _mock_rc_client(entitlements):
    """Build a mock httpx.AsyncClient whose GET returns a subscriber payload."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"subscriber": {"entitlements": entitlements}}
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    return mock_client

@pytest.mark.asyncio
async def test_entitlement_expired_subscription_is_inactive():
    """An expires_date in the past must NOT grant Pro — presence of the field alone
    is not enough (revenue bug: expired subscribers kept access)."""
    from ditare_api.main import check_pro_entitlement

    rc = _mock_rc_client({"pro": {"expires_date": "2020-01-01T00:00:00Z"}})
    with patch.object(settings, "env", "production"), \
         patch.object(settings, "revenuecat_api_key", "rc-key"), \
         patch("ditare_api.main.get_user_id", return_value="user-expired"), \
         patch("ditare_api.main.httpx.AsyncClient", return_value=rc):
        redis_client.delete("entitlement:pro:cache:user-expired")
        redis_client.delete("entitlement:pro:user-expired")
        assert await check_pro_entitlement("tok") is False
        assert redis_client.get("entitlement:pro:cache:user-expired") == "inactive"
        redis_client.delete("entitlement:pro:cache:user-expired")

@pytest.mark.asyncio
async def test_entitlement_future_expiry_is_active():
    """A future expires_date grants Pro and is cached as active."""
    from ditare_api.main import check_pro_entitlement

    rc = _mock_rc_client({"pro": {"expires_date": "2099-01-01T00:00:00Z"}})
    with patch.object(settings, "env", "production"), \
         patch.object(settings, "revenuecat_api_key", "rc-key"), \
         patch("ditare_api.main.get_user_id", return_value="user-future"), \
         patch("ditare_api.main.httpx.AsyncClient", return_value=rc):
        redis_client.delete("entitlement:pro:cache:user-future")
        assert await check_pro_entitlement("tok") is True
        assert redis_client.get("entitlement:pro:cache:user-future") == "active"
        redis_client.delete("entitlement:pro:cache:user-future")

@pytest.mark.asyncio
async def test_entitlement_lifetime_null_expiry_is_active():
    """RevenueCat lifetime/promotional entitlements have expires_date=null and
    must be treated as active when the pro entitlement exists."""
    from ditare_api.main import check_pro_entitlement

    rc = _mock_rc_client({"pro": {"expires_date": None}})
    with patch.object(settings, "env", "production"), \
         patch.object(settings, "revenuecat_api_key", "rc-key"), \
         patch("ditare_api.main.get_user_id", return_value="user-lifetime"), \
         patch("ditare_api.main.httpx.AsyncClient", return_value=rc):
        redis_client.delete("entitlement:pro:cache:user-lifetime")
        assert await check_pro_entitlement("tok") is True
        redis_client.delete("entitlement:pro:cache:user-lifetime")

@pytest.mark.asyncio
async def test_entitlement_no_pro_entitlement_is_inactive():
    """A subscriber with no 'pro' entitlement at all is not Pro."""
    from ditare_api.main import check_pro_entitlement

    rc = _mock_rc_client({})
    with patch.object(settings, "env", "production"), \
         patch.object(settings, "revenuecat_api_key", "rc-key"), \
         patch("ditare_api.main.get_user_id", return_value="user-none"), \
         patch("ditare_api.main.httpx.AsyncClient", return_value=rc):
        redis_client.delete("entitlement:pro:cache:user-none")
        assert await check_pro_entitlement("tok") is False
        redis_client.delete("entitlement:pro:cache:user-none")

@pytest.mark.asyncio
async def test_api_cache_does_not_clobber_webhook_entitlement():
    """The 5-minute API cache must live under its own key so it never overwrites
    the persistent webhook-set entitlement (entitlement:pro:<user_id>)."""
    from ditare_api.main import check_pro_entitlement

    rc = _mock_rc_client({"pro": {"expires_date": "2020-01-01T00:00:00Z"}})
    with patch.object(settings, "env", "production"), \
         patch.object(settings, "revenuecat_api_key", "rc-key"), \
         patch("ditare_api.main.get_user_id", return_value="user-sep"), \
         patch("ditare_api.main.httpx.AsyncClient", return_value=rc):
        redis_client.delete("entitlement:pro:cache:user-sep")
        redis_client.set("entitlement:pro:user-sep", "active")  # persistent webhook state
        await check_pro_entitlement("tok")
        # Webhook state untouched, no TTL gained
        assert redis_client.get("entitlement:pro:user-sep") == "active"
        assert redis_client.ttl("entitlement:pro:user-sep") == -1
        redis_client.delete("entitlement:pro:user-sep")
        redis_client.delete("entitlement:pro:cache:user-sep")

def test_generate_session_token_requires_jwt_secret():
    """generate_session_token must hard-fail when JWT_SECRET is unset instead of
    silently signing with a throwaway secret (BLOCKER from CHRA-2499)."""
    from ditare_api.main import generate_session_token

    with patch.object(settings, "jwt_secret", ""):
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            generate_session_token("u1", "apple-sub-1")
