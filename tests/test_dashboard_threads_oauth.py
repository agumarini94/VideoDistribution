"""
Tests for Phase 29f: in-browser Threads OAuth connect flow
(GET /api/oauth/threads/start + /api/oauth/threads/callback,
dashboard/api.py). Uses FastAPI's TestClient (real HTTP + middleware +
cookies), same reasoning as tests/test_dashboard_pinterest_oauth.py (Phase
29e) — this is request-level behavior (the signed state param round-
tripping through an unauthenticated redirect), not something calling route
functions directly would exercise.

app/publishers/threads.py's build_authorization_url/
exchange_code_for_credentials are never actually called against the real
Threads API: they're monkeypatched on dashboard_api.threads_publisher, same
spirit as every other platform in this suite being exercised only against
mocked HTTP.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard import api as dashboard_api
from app.auth import create_oauth_state_token, hash_password
from app.exceptions import PermanentError
from app.models import Account, Client, User

ADMIN_AUTH = (dashboard_api._DASHBOARD_USERNAME, dashboard_api._DASHBOARD_PASSWORD)

_FAKE_AUTH_URL = "https://www.threads.net/oauth/authorize?fake=1"


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
        resp = client.get("/api/oauth/threads/start", follow_redirects=False)
        assert resp.status_code == 401

    def test_admin_basic_auth_is_403(self, client):
        resp = client.get("/api/oauth/threads/start", auth=ADMIN_AUTH, follow_redirects=False)
        assert resp.status_code == 403

    def test_client_user_redirects_to_threads_authorize_url(self, client, db_session, monkeypatch):
        c = _make_client(db_session)
        _make_client_user(db_session, c)
        _login(client, "user@acme.test", "password123")

        captured = {}

        def fake_build_authorization_url(redirect_uri, state):
            captured["redirect_uri"] = redirect_uri
            captured["state"] = state
            return _FAKE_AUTH_URL

        monkeypatch.setattr(dashboard_api.threads_publisher, "build_authorization_url", fake_build_authorization_url)

        resp = client.get("/api/oauth/threads/start", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == _FAKE_AUTH_URL
        assert captured["redirect_uri"].endswith("/api/oauth/threads/callback")

        # No PKCE verifier in Threads' state — same shape as YouTube's/Meta's/Pinterest's.
        from app.auth import verify_oauth_state_token

        decoded = verify_oauth_state_token(captured["state"])
        assert decoded["client_id"] == c.id
        assert "code_verifier" not in decoded


class TestCallbackRoute:
    def test_threads_error_param_redirects_with_reason(self, client):
        resp = client.get("/api/oauth/threads/callback?error=access_denied", follow_redirects=False)
        assert resp.status_code in (302, 307)
        location = resp.headers["location"]
        assert "threads_connect=error" in location
        assert "reason=access_denied" in location

    def test_missing_code_or_state_redirects_with_reason(self, client):
        resp = client.get("/api/oauth/threads/callback", follow_redirects=False)
        assert "reason=missing_code_or_state" in resp.headers["location"]

    def test_tampered_state_redirects_with_reason(self, client):
        resp = client.get(
            "/api/oauth/threads/callback",
            params={"code": "auth-code", "state": "not-a-real-signed-token"},
            follow_redirects=False,
        )
        assert "reason=invalid_or_expired_state" in resp.headers["location"]

    def test_state_for_nonexistent_client_redirects_with_reason(self, client):
        state = create_oauth_state_token({"client_id": 999999, "user_id": 1})
        resp = client.get(
            "/api/oauth/threads/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=unknown_client" in resp.headers["location"]

    def test_happy_path_creates_account(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        monkeypatch.setattr(
            dashboard_api.threads_publisher,
            "exchange_code_for_credentials",
            lambda code, redirect_uri: {
                "threads_user_id": "threads-user-1",
                "username": "bloomstudio",
                "access_token": "access-1",
                "expires_at": "2026-12-01T00:00:00+00:00",
            },
        )

        resp = client.get(
            "/api/oauth/threads/callback",
            params={"code": "auth-code-123", "state": state},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 307)
        assert "threads_connect=success" in resp.headers["location"]

        account = db_session.query(Account).filter(Account.platform == "threads", Account.client_id == c.id).one()
        assert account.credentials["threads_user_id"] == "threads-user-1"
        assert account.credentials["access_token"] == "access-1"
        assert account.is_active is True
        assert account.name == "Bloom Studio (self-service)"

    def test_reconnecting_rotates_existing_account_instead_of_duplicating(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")

        state1 = create_oauth_state_token({"client_id": c.id, "user_id": 1})
        monkeypatch.setattr(
            dashboard_api.threads_publisher,
            "exchange_code_for_credentials",
            lambda code, redirect_uri: {
                "threads_user_id": "threads-user-1",
                "username": "bloomstudio",
                "access_token": "access-1",
                "expires_at": None,
            },
        )
        client.get("/api/oauth/threads/callback", params={"code": "first-code", "state": state1}, follow_redirects=False)

        state2 = create_oauth_state_token({"client_id": c.id, "user_id": 1})
        monkeypatch.setattr(
            dashboard_api.threads_publisher,
            "exchange_code_for_credentials",
            lambda code, redirect_uri: {
                "threads_user_id": "threads-user-1",
                "username": "bloomstudio",
                "access_token": "access-2-rotated",
                "expires_at": None,
            },
        )
        client.get("/api/oauth/threads/callback", params={"code": "second-code", "state": state2}, follow_redirects=False)

        accounts = db_session.query(Account).filter(Account.platform == "threads", Account.client_id == c.id).all()
        assert len(accounts) == 1
        assert accounts[0].credentials["access_token"] == "access-2-rotated"

    def test_permanent_error_during_exchange_redirects_with_reason(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        def fake_exchange(code, redirect_uri):
            raise PermanentError("Threads rejected the authorization code")

        monkeypatch.setattr(dashboard_api.threads_publisher, "exchange_code_for_credentials", fake_exchange)

        resp = client.get(
            "/api/oauth/threads/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=exchange_failed" in resp.headers["location"]
        assert db_session.query(Account).filter(Account.platform == "threads", Account.client_id == c.id).count() == 0
