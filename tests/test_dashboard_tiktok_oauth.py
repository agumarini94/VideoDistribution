"""
Tests for Phase 29c: in-browser TikTok OAuth connect flow
(GET /api/oauth/tiktok/start + /api/oauth/tiktok/callback,
dashboard/api.py). Uses FastAPI's TestClient (real HTTP + middleware +
cookies), same reasoning as tests/test_dashboard_twitter_oauth.py (Phase
29b) — this is request-level behavior (the signed state param round-tripping
through an unauthenticated redirect, including the PKCE code_verifier it now
also carries), not something calling route functions directly would
exercise.

TikTok's OAuth endpoints are never actually called:
app/publishers/tiktok.py's build_authorization_url/exchange_code_for_credentials
are monkeypatched, same spirit as every other publisher in this suite being
exercised only against mocked HTTP.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard import api as dashboard_api
from app.auth import create_oauth_state_token, hash_password
from app.models import Account, Client, User

ADMIN_AUTH = (dashboard_api._DASHBOARD_USERNAME, dashboard_api._DASHBOARD_PASSWORD)

_FAKE_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/?fake=1"
_FAKE_CREDENTIALS = {
    "access_token": "fake-access-token",
    "refresh_token": "fake-refresh-token",
    "open_id": "fake-open-id",
    "scope": "user.info.basic,video.upload",
    "expires_at": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture(autouse=True)
def _no_real_dispatch(monkeypatch):
    monkeypatch.setattr(dashboard_api.publish_job, "delay", lambda job_id: None)


@pytest.fixture
def client():
    return TestClient(dashboard_api.app)


def _make_client(db_session, name="Acme Co") -> Client:
    c = Client(name=name, kind="client")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


def _make_client_user(db_session, client_row, email="user@acme.test", password="password123") -> User:
    user = User(
        email=email,
        hashed_password=hash_password(password),
        role="client_user",
        client_id=client_row.id,
        is_approved=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _login(client, email, password):
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp


class TestStartRoute:
    def test_anonymous_is_401(self, client):
        resp = client.get("/api/oauth/tiktok/start", follow_redirects=False)
        assert resp.status_code == 401

    def test_admin_basic_auth_is_403(self, client):
        resp = client.get("/api/oauth/tiktok/start", auth=ADMIN_AUTH, follow_redirects=False)
        assert resp.status_code == 403

    def test_client_user_redirects_to_tiktok_authorize_url_with_pkce(self, client, db_session, monkeypatch):
        c = _make_client(db_session)
        _make_client_user(db_session, c)
        _login(client, "user@acme.test", "password123")

        captured = {}

        def fake_build_authorization_url(redirect_uri, state, code_challenge):
            captured["redirect_uri"] = redirect_uri
            captured["state"] = state
            captured["code_challenge"] = code_challenge
            return _FAKE_AUTH_URL

        monkeypatch.setattr(dashboard_api.tiktok_publisher, "build_authorization_url", fake_build_authorization_url)

        resp = client.get("/api/oauth/tiktok/start", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == _FAKE_AUTH_URL
        assert captured["redirect_uri"].endswith("/api/oauth/tiktok/callback")
        assert captured["code_challenge"]  # a non-empty PKCE challenge was generated

        # The state param encodes this client_user's own client_id AND the
        # PKCE verifier paired with the challenge above — verified
        # independently via the same helper the callback route uses.
        from app.auth import verify_oauth_state_token

        decoded = verify_oauth_state_token(captured["state"])
        assert decoded["client_id"] == c.id
        assert decoded["code_verifier"]

        # The challenge sent to TikTok must actually be derived from the
        # verifier stashed in state via TikTok's non-standard HEX digest
        # (not standard RFC 7636 base64url — see
        # scripts/authorize_tiktok.py::_generate_pkce_pair).
        import hashlib

        expected_challenge = hashlib.sha256(decoded["code_verifier"].encode("ascii")).hexdigest()
        assert captured["code_challenge"] == expected_challenge


class TestCallbackRoute:
    def test_tiktok_error_param_redirects_with_reason(self, client):
        resp = client.get("/api/oauth/tiktok/callback?error=access_denied", follow_redirects=False)
        assert resp.status_code in (302, 307)
        location = resp.headers["location"]
        assert "tiktok_connect=error" in location
        assert "reason=access_denied" in location

    def test_missing_code_or_state_redirects_with_reason(self, client):
        resp = client.get("/api/oauth/tiktok/callback", follow_redirects=False)
        assert "reason=missing_code_or_state" in resp.headers["location"]

    def test_tampered_state_redirects_with_reason(self, client):
        resp = client.get(
            "/api/oauth/tiktok/callback",
            params={"code": "auth-code", "state": "not-a-real-signed-token"},
            follow_redirects=False,
        )
        assert "reason=invalid_or_expired_state" in resp.headers["location"]

    def test_state_missing_pkce_verifier_redirects_with_reason(self, client, db_session):
        # A validly-signed state token that happens not to carry a
        # code_verifier (e.g. an old-format token) — distinct from a
        # tampered/expired signature, and distinct from TikTok rejecting a
        # verifier/challenge mismatch at the token endpoint (that's the
        # "exchange_failed" case below).
        c = _make_client(db_session)
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})
        resp = client.get(
            "/api/oauth/tiktok/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=missing_pkce_verifier" in resp.headers["location"]

    def test_state_for_nonexistent_client_redirects_with_reason(self, client):
        state = create_oauth_state_token({"client_id": 999999, "user_id": 1, "code_verifier": "verifier123"})
        resp = client.get(
            "/api/oauth/tiktok/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=unknown_client" in resp.headers["location"]

    def test_happy_path_creates_account_scoped_to_client(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1, "code_verifier": "verifier123"})

        captured = {}

        def fake_exchange(code, redirect_uri, code_verifier):
            captured["code"] = code
            captured["redirect_uri"] = redirect_uri
            captured["code_verifier"] = code_verifier
            return dict(_FAKE_CREDENTIALS)

        monkeypatch.setattr(dashboard_api.tiktok_publisher, "exchange_code_for_credentials", fake_exchange)

        resp = client.get(
            "/api/oauth/tiktok/callback",
            params={"code": "auth-code-123", "state": state},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 307)
        assert "tiktok_connect=success" in resp.headers["location"]
        assert captured["code"] == "auth-code-123"
        assert captured["code_verifier"] == "verifier123"
        assert captured["redirect_uri"].endswith("/api/oauth/tiktok/callback")

        account = db_session.query(Account).filter(Account.platform == "tiktok", Account.client_id == c.id).one()
        assert account.credentials == _FAKE_CREDENTIALS
        assert account.is_active is True

    def test_reconnecting_rotates_existing_account_instead_of_duplicating(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")

        state1 = create_oauth_state_token({"client_id": c.id, "user_id": 1, "code_verifier": "verifier-1"})
        monkeypatch.setattr(
            dashboard_api.tiktok_publisher,
            "exchange_code_for_credentials",
            lambda code, redirect_uri, code_verifier: dict(_FAKE_CREDENTIALS),
        )
        client.get("/api/oauth/tiktok/callback", params={"code": "first-code", "state": state1}, follow_redirects=False)

        state2 = create_oauth_state_token({"client_id": c.id, "user_id": 1, "code_verifier": "verifier-2"})
        rotated_credentials = {**_FAKE_CREDENTIALS, "access_token": "rotated-access-token"}
        monkeypatch.setattr(
            dashboard_api.tiktok_publisher,
            "exchange_code_for_credentials",
            lambda code, redirect_uri, code_verifier: rotated_credentials,
        )
        client.get("/api/oauth/tiktok/callback", params={"code": "second-code", "state": state2}, follow_redirects=False)

        accounts = db_session.query(Account).filter(Account.platform == "tiktok", Account.client_id == c.id).all()
        assert len(accounts) == 1
        assert accounts[0].credentials["access_token"] == "rotated-access-token"

    def test_pkce_verifier_mismatch_at_exchange_redirects_with_reason(self, client, db_session, monkeypatch):
        # Simulates TikTok rejecting the exchange because the code_verifier
        # doesn't hash to the code_challenge it received earlier —
        # app/publishers/tiktok.py::exchange_code_for_credentials
        # normalizes any token-endpoint rejection to PermanentError (see its
        # docstring), so this is exercised the same way as any other
        # exchange failure.
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1, "code_verifier": "wrong-verifier"})

        from app.exceptions import PermanentError

        def fake_exchange(code, redirect_uri, code_verifier):
            raise PermanentError("TikTok token endpoint rejected the request (invalid_grant): code_verifier mismatch")

        monkeypatch.setattr(dashboard_api.tiktok_publisher, "exchange_code_for_credentials", fake_exchange)

        resp = client.get(
            "/api/oauth/tiktok/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=exchange_failed" in resp.headers["location"]
        assert db_session.query(Account).filter(Account.platform == "tiktok").count() == 0

    def test_exchange_failure_redirects_with_reason(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1, "code_verifier": "verifier123"})

        from app.exceptions import PermanentError

        def fake_exchange(code, redirect_uri, code_verifier):
            raise PermanentError("token endpoint rejected the code")

        monkeypatch.setattr(dashboard_api.tiktok_publisher, "exchange_code_for_credentials", fake_exchange)

        resp = client.get(
            "/api/oauth/tiktok/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=exchange_failed" in resp.headers["location"]
        assert db_session.query(Account).filter(Account.platform == "tiktok").count() == 0
