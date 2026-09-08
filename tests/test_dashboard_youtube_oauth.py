"""
Tests for Phase 29a: in-browser YouTube OAuth connect flow
(GET /api/oauth/youtube/start + /api/oauth/youtube/callback,
dashboard/api.py). Uses FastAPI's TestClient (real HTTP + middleware +
cookies), same reasoning as tests/test_dashboard_auth.py — this is
request-level behavior (the signed state param round-tripping through an
unauthenticated redirect), not something calling route functions directly
would exercise.

Google's OAuth endpoints are never actually called: app/publishers/youtube.py's
build_authorization_url/exchange_code_for_credentials are monkeypatched, same
spirit as every other publisher in this suite being exercised only against
mocked HTTP.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard import api as dashboard_api
from app.auth import create_oauth_state_token, hash_password
from app.models import Account, Client, User

ADMIN_AUTH = (dashboard_api._DASHBOARD_USERNAME, dashboard_api._DASHBOARD_PASSWORD)

_FAKE_AUTH_URL = "https://accounts.google.com/o/oauth2/auth?fake=1"
_FAKE_CREDENTIALS = {
    "token": "fake-access-token",
    "refresh_token": "fake-refresh-token",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "fake-client-id",
    "client_secret": "fake-client-secret",
    "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
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
        resp = client.get("/api/oauth/youtube/start", follow_redirects=False)
        assert resp.status_code == 401

    def test_admin_basic_auth_is_403(self, client):
        resp = client.get("/api/oauth/youtube/start", auth=ADMIN_AUTH, follow_redirects=False)
        assert resp.status_code == 403

    def test_client_user_redirects_to_google_authorize_url(self, client, db_session, monkeypatch):
        c = _make_client(db_session)
        _make_client_user(db_session, c)
        _login(client, "user@acme.test", "password123")

        captured = {}

        def fake_build_authorization_url(redirect_uri, state):
            captured["redirect_uri"] = redirect_uri
            captured["state"] = state
            return _FAKE_AUTH_URL

        monkeypatch.setattr(dashboard_api.youtube_publisher, "build_authorization_url", fake_build_authorization_url)

        resp = client.get("/api/oauth/youtube/start", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == _FAKE_AUTH_URL
        assert captured["redirect_uri"].endswith("/api/oauth/youtube/callback")

        # The state param encodes this client_user's own client_id — verified
        # independently via the same helper the callback route uses.
        from app.auth import verify_oauth_state_token

        decoded = verify_oauth_state_token(captured["state"])
        assert decoded["client_id"] == c.id


class TestCallbackRoute:
    def test_google_error_param_redirects_with_reason(self, client):
        resp = client.get("/api/oauth/youtube/callback?error=access_denied", follow_redirects=False)
        assert resp.status_code in (302, 307)
        location = resp.headers["location"]
        assert "youtube_connect=error" in location
        assert "reason=access_denied" in location

    def test_missing_code_or_state_redirects_with_reason(self, client):
        resp = client.get("/api/oauth/youtube/callback", follow_redirects=False)
        assert "reason=missing_code_or_state" in resp.headers["location"]

    def test_tampered_state_redirects_with_reason(self, client):
        resp = client.get(
            "/api/oauth/youtube/callback",
            params={"code": "auth-code", "state": "not-a-real-signed-token"},
            follow_redirects=False,
        )
        assert "reason=invalid_or_expired_state" in resp.headers["location"]

    def test_state_for_nonexistent_client_redirects_with_reason(self, client):
        state = create_oauth_state_token({"client_id": 999999, "user_id": 1})
        resp = client.get(
            "/api/oauth/youtube/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=unknown_client" in resp.headers["location"]

    def test_happy_path_creates_account_scoped_to_client(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        captured = {}

        def fake_exchange(code, redirect_uri):
            captured["code"] = code
            captured["redirect_uri"] = redirect_uri
            return dict(_FAKE_CREDENTIALS)

        monkeypatch.setattr(dashboard_api.youtube_publisher, "exchange_code_for_credentials", fake_exchange)

        resp = client.get(
            "/api/oauth/youtube/callback",
            params={"code": "auth-code-123", "state": state},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 307)
        assert "youtube_connect=success" in resp.headers["location"]
        assert captured["code"] == "auth-code-123"
        assert captured["redirect_uri"].endswith("/api/oauth/youtube/callback")

        account = db_session.query(Account).filter(Account.platform == "youtube", Account.client_id == c.id).one()
        assert account.credentials == _FAKE_CREDENTIALS
        assert account.is_active is True

    def test_reconnecting_rotates_existing_account_instead_of_duplicating(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        monkeypatch.setattr(
            dashboard_api.youtube_publisher, "exchange_code_for_credentials", lambda code, redirect_uri: dict(_FAKE_CREDENTIALS)
        )
        client.get("/api/oauth/youtube/callback", params={"code": "first-code", "state": state}, follow_redirects=False)

        rotated_credentials = {**_FAKE_CREDENTIALS, "token": "rotated-access-token"}
        monkeypatch.setattr(
            dashboard_api.youtube_publisher, "exchange_code_for_credentials", lambda code, redirect_uri: rotated_credentials
        )
        client.get("/api/oauth/youtube/callback", params={"code": "second-code", "state": state}, follow_redirects=False)

        accounts = db_session.query(Account).filter(Account.platform == "youtube", Account.client_id == c.id).all()
        assert len(accounts) == 1
        assert accounts[0].credentials["token"] == "rotated-access-token"

    def test_exchange_failure_redirects_with_reason(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        from app.exceptions import PermanentError

        def fake_exchange(code, redirect_uri):
            raise PermanentError("token endpoint rejected the code")

        monkeypatch.setattr(dashboard_api.youtube_publisher, "exchange_code_for_credentials", fake_exchange)

        resp = client.get(
            "/api/oauth/youtube/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=exchange_failed" in resp.headers["location"]
        assert db_session.query(Account).filter(Account.platform == "youtube").count() == 0
