import pytest
import time
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from ditare_api.main import app, settings

client = TestClient(app)


class TestAuthExchange:
    """Tests for POST /auth/exchange Apple identity token exchange."""

    @patch.object(settings, "apple_team_id", "")
    @patch.object(settings, "apple_bundle_id", "")
    def test_auth_exchange_not_configured(self):
        """Should return 501 when APPLE_TEAM_ID or APPLE_BUNDLE_ID are unset."""
        resp = client.post("/auth/exchange", json={
            "identity_token": "test",
            "apple_user_id": "test"
        })
        assert resp.status_code == 501
        assert "Apple auth not yet configured" in resp.json()["detail"]

    @patch.object(settings, "apple_team_id", "TEAM123")
    @patch.object(settings, "apple_bundle_id", "com.ditare.app")
    def test_auth_exchange_missing_token(self):
        """Should return 401 when identity_token is missing."""
        resp = client.post("/auth/exchange", json={
            "identity_token": "",
            "apple_user_id": "user123"
        })
        assert resp.status_code == 401
        assert "Missing" in resp.json()["detail"]

    @patch.object(settings, "apple_team_id", "TEAM123")
    @patch.object(settings, "apple_bundle_id", "com.ditare.app")
    def test_auth_exchange_missing_user_id(self):
        """Should return 401 when apple_user_id is missing."""
        resp = client.post("/auth/exchange", json={
            "identity_token": "some.token.here",
            "apple_user_id": ""
        })
        assert resp.status_code == 401
        assert "Missing" in resp.json()["detail"]

    @patch("ditare_api.main.verify_apple_token")
    @patch("ditare_api.main.upsert_user")
    @patch("ditare_api.main.generate_session_token")
    @patch("ditare_api.main.redis_client")
    @patch.object(settings, "apple_team_id", "TEAM123")
    @patch.object(settings, "apple_bundle_id", "com.ditare.app")
    def test_auth_exchange_success(
        self, mock_redis, mock_gen_token, mock_upsert, mock_verify
    ):
        """Should return session token on successful Apple auth."""
        mock_verify.return_value = {"sub": "user123", "email": "test@example.com"}
        mock_gen_token.return_value = "session_token_abc"

        resp = client.post("/auth/exchange", json={
            "identity_token": "valid.apple.token",
            "apple_user_id": "user123",
            "email": "test@example.com",
            "full_name": "Test User"
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session_token"] == "session_token_abc"
        assert data["expires_in"] == 2592000
        assert data["user_id"] == "user123"

        mock_verify.assert_called_once_with("valid.apple.token", "user123", "com.ditare.app")
        mock_upsert.assert_called_once_with("user123", "test@example.com", "Test User")
        mock_gen_token.assert_called_once_with("user123", "user123")
        mock_redis.setex.assert_called_once_with("token:user:session_token_abc", 2592000, "user123")

    @patch("ditare_api.main.jwt.decode")
    @patch("ditare_api.main.PyJWKClient")
    @patch.object(settings, "apple_team_id", "TEAM123")
    @patch.object(settings, "apple_bundle_id", "com.ditare.app")
    def test_verify_apple_token_invalid_signature(self, mock_jwks_client, mock_jwt_decode):
        """Should return 401 when Apple token signature is invalid."""
        from jwt.exceptions import InvalidSignatureError
        mock_jwks_client_instance = MagicMock()
        mock_jwks_client.return_value = mock_jwks_client_instance
        mock_signing_key = MagicMock()
        mock_signing_key.key = "fake_key"
        mock_jwks_client_instance.get_signing_key_from_jwt.return_value = mock_signing_key
        mock_jwt_decode.side_effect = InvalidSignatureError("Invalid signature")

        resp = client.post("/auth/exchange", json={
            "identity_token": "invalid.token.here",
            "apple_user_id": "user123"
        })

        assert resp.status_code == 401
        assert "Invalid Apple identity token" in resp.json()["detail"]

    @patch("ditare_api.main.jwt.decode")
    @patch("ditare_api.main.PyJWKClient")
    @patch.object(settings, "apple_team_id", "TEAM123")
    @patch.object(settings, "apple_bundle_id", "com.ditare.app")
    def test_verify_apple_token_sub_mismatch(self, mock_jwks_client, mock_jwt_decode):
        """Should return 401 when sub claim doesn't match apple_user_id."""
        mock_jwks_client_instance = MagicMock()
        mock_jwks_client.return_value = mock_jwks_client_instance
        mock_signing_key = MagicMock()
        mock_signing_key.key = "fake_key"
        mock_jwks_client_instance.get_signing_key_from_jwt.return_value = mock_signing_key
        mock_jwt_decode.return_value = {"sub": "different_user"}

        resp = client.post("/auth/exchange", json={
            "identity_token": "valid.token.here",
            "apple_user_id": "user123"
        })

        assert resp.status_code == 401
        assert "sub claim does not match" in resp.json()["detail"]

    @patch("ditare_api.main.jwt.decode")
    @patch("ditare_api.main.PyJWKClient")
    @patch.object(settings, "apple_team_id", "TEAM123")
    @patch.object(settings, "apple_bundle_id", "com.ditare.app")
    def test_verify_apple_token_expired(self, mock_jwks_client, mock_jwt_decode):
        """Should return 401 when Apple token is expired."""
        from jwt.exceptions import ExpiredSignatureError
        mock_jwks_client_instance = MagicMock()
        mock_jwks_client.return_value = mock_jwks_client_instance
        mock_signing_key = MagicMock()
        mock_signing_key.key = "fake_key"
        mock_jwks_client_instance.get_signing_key_from_jwt.return_value = mock_signing_key
        mock_jwt_decode.side_effect = ExpiredSignatureError("Token expired")

        resp = client.post("/auth/exchange", json={
            "identity_token": "expired.token.here",
            "apple_user_id": "user123"
        })

        assert resp.status_code == 401
        assert "Invalid Apple identity token" in resp.json()["detail"]

    @patch("ditare_api.main.jwt.decode")
    @patch("ditare_api.main.PyJWKClient")
    @patch.object(settings, "apple_team_id", "TEAM123")
    @patch.object(settings, "apple_bundle_id", "com.ditare.app")
    def test_verify_apple_token_aud_mismatch(self, mock_jwks_client, mock_jwt_decode):
        """Should return 401 when aud claim doesn't match bundle ID."""
        from jwt.exceptions import InvalidAudienceError
        mock_jwks_client_instance = MagicMock()
        mock_jwks_client.return_value = mock_jwks_client_instance
        mock_signing_key = MagicMock()
        mock_signing_key.key = "fake_key"
        mock_jwks_client_instance.get_signing_key_from_jwt.return_value = mock_signing_key
        mock_jwt_decode.side_effect = InvalidAudienceError("Invalid audience")

        resp = client.post("/auth/exchange", json={
            "identity_token": "valid.token.here",
            "apple_user_id": "user123"
        })

        assert resp.status_code == 401
        assert "Invalid Apple identity token" in resp.json()["detail"]


class TestAuthExchangeIntegration:
    """Integration-style tests with mocked Redis."""

    @patch("ditare_api.main.jwt.decode")
    @patch("ditare_api.main.PyJWKClient")
    @patch("ditare_api.main.redis_client")
    @patch.object(settings, "apple_team_id", "TEAM123")
    @patch.object(settings, "apple_bundle_id", "com.ditare.app")
    @patch.object(settings, "jwt_secret", "this_is_a_very_secure_test_secret_key_32b")
    def test_full_flow_with_redis(
        self, mock_redis, mock_jwks_client, mock_jwt_decode
    ):
        """Test full auth flow including Redis storage."""
        mock_jwks_client_instance = MagicMock()
        mock_jwks_client.return_value = mock_jwks_client_instance
        mock_signing_key = MagicMock()
        mock_signing_key.key = "fake_key"
        mock_jwks_client_instance.get_signing_key_from_jwt.return_value = mock_signing_key
        mock_jwt_decode.return_value = {"sub": "user123"}
        mock_redis.setex.return_value = True

        resp = client.post("/auth/exchange", json={
            "identity_token": "valid.apple.token",
            "apple_user_id": "user123",
            "email": "user@example.com",
            "full_name": "John Doe"
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["user_id"] == "user123"
        assert "session_token" in data
        assert data["expires_in"] == 2592000

        # Verify Redis calls
        mock_redis.hset.assert_called_once()
        mock_redis.setex.assert_called_once()

    @patch("ditare_api.main.jwt.decode")
    @patch("ditare_api.main.PyJWKClient")
    @patch("ditare_api.main.redis_client")
    @patch.object(settings, "apple_team_id", "TEAM123")
    @patch.object(settings, "apple_bundle_id", "com.ditare.app")
    @patch.object(settings, "jwt_secret", "this_is_a_very_secure_test_secret_key_32b")
    def test_user_without_optional_fields(
        self, mock_redis, mock_jwks_client, mock_jwt_decode
    ):
        """Test auth flow when email and full_name are not provided."""
        mock_jwks_client_instance = MagicMock()
        mock_jwks_client.return_value = mock_jwks_client_instance
        mock_signing_key = MagicMock()
        mock_signing_key.key = "fake_key"
        mock_jwks_client_instance.get_signing_key_from_jwt.return_value = mock_signing_key
        mock_jwt_decode.return_value = {"sub": "anon_user"}

        resp = client.post("/auth/exchange", json={
            "identity_token": "valid.token",
            "apple_user_id": "anon_user"
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["user_id"] == "anon_user"

        # Verify hset was called with minimal data
        mock_redis.hset.assert_called_once()
        call_args = mock_redis.hset.call_args
        assert call_args.args[0] == "user:anon_user"
        assert "apple_user_id" in call_args.kwargs["mapping"]
