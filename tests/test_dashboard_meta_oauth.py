"""
Tests for Phase 29d: in-browser Facebook + Instagram OAuth connect flow
(GET /api/oauth/meta/start + /api/oauth/meta/callback, dashboard/api.py).
Uses FastAPI's TestClient (real HTTP + middleware + cookies), same reasoning
as tests/test_dashboard_twitter_oauth.py (Phase 29b) — this is request-level
behavior (the signed state param round-tripping through an unauthenticated
redirect), not something calling route functions directly would exercise.

Meta's OAuth chain functions (exchange_code_for_user_token,
exchange_long_lived_token, list_pages, get_instagram_business_account) are
never actually called against the real Graph API: they're monkeypatched on
app/publishers/meta.py, same spirit as every other publisher in this suite
being exercised only against mocked HTTP.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard import api as dashboard_api
from app.auth import create_oauth_state_token, hash_password
from app.exceptions import PermanentError, TransientError
from app.models import Account, Client, User

ADMIN_AUTH = (dashboard_api._DASHBOARD_USERNAME, dashboard_api._DASHBOARD_PASSWORD)

_FAKE_AUTH_URL = "https://www.facebook.com/v26.0/dialog/oauth?fake=1"


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


def _mock_chain(monkeypatch, pages, ig_by_page=None):
    """
    Monkeypatches the four OAuth-chain functions the callback calls:
    exchange_code_for_user_token -> exchange_long_lived_token -> list_pages
    -> get_instagram_business_account (once per page). `ig_by_page` maps
    page_id -> ig_user_id (or omits it -> None, i.e. no linked IG account).
    """
    ig_by_page = ig_by_page or {}
    monkeypatch.setattr(
        dashboard_api.meta_publisher,
        "exchange_code_for_user_token",
        lambda code, redirect_uri: {"access_token": "short-lived-token", "expires_in": 3600},
    )
    monkeypatch.setattr(
        dashboard_api.meta_publisher,
        "exchange_long_lived_token",
        lambda short_lived_token: {"access_token": "long-lived-token", "expires_at": "2026-01-01T00:00:00+00:00"},
    )
    monkeypatch.setattr(dashboard_api.meta_publisher, "list_pages", lambda user_token: pages)
    monkeypatch.setattr(
        dashboard_api.meta_publisher,
        "get_instagram_business_account",
        lambda page_id, page_token: ig_by_page.get(page_id),
    )


class TestStartRoute:
    def test_anonymous_is_401(self, client):
        resp = client.get("/api/oauth/meta/start", follow_redirects=False)
        assert resp.status_code == 401

    def test_admin_basic_auth_is_403(self, client):
        resp = client.get("/api/oauth/meta/start", auth=ADMIN_AUTH, follow_redirects=False)
        assert resp.status_code == 403

    def test_client_user_redirects_to_meta_authorize_url(self, client, db_session, monkeypatch):
        c = _make_client(db_session)
        _make_client_user(db_session, c)
        _login(client, "user@acme.test", "password123")

        captured = {}

        def fake_build_authorization_url(redirect_uri, state):
            captured["redirect_uri"] = redirect_uri
            captured["state"] = state
            return _FAKE_AUTH_URL

        monkeypatch.setattr(dashboard_api.meta_publisher, "build_authorization_url", fake_build_authorization_url)

        resp = client.get("/api/oauth/meta/start", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == _FAKE_AUTH_URL
        assert captured["redirect_uri"].endswith("/api/oauth/meta/callback")

        # The state param encodes this client_user's own client_id — no
        # PKCE verifier here, unlike Twitter's/TikTok's state (Meta's OAuth
        # dialog doesn't require PKCE).
        from app.auth import verify_oauth_state_token

        decoded = verify_oauth_state_token(captured["state"])
        assert decoded["client_id"] == c.id
        assert "code_verifier" not in decoded


class TestCallbackRoute:
    def test_meta_error_param_redirects_with_reason(self, client):
        resp = client.get("/api/oauth/meta/callback?error=access_denied", follow_redirects=False)
        assert resp.status_code in (302, 307)
        location = resp.headers["location"]
        assert "meta_connect=error" in location
        assert "reason=access_denied" in location

    def test_missing_code_or_state_redirects_with_reason(self, client):
        resp = client.get("/api/oauth/meta/callback", follow_redirects=False)
        assert "reason=missing_code_or_state" in resp.headers["location"]

    def test_tampered_state_redirects_with_reason(self, client):
        resp = client.get(
            "/api/oauth/meta/callback",
            params={"code": "auth-code", "state": "not-a-real-signed-token"},
            follow_redirects=False,
        )
        assert "reason=invalid_or_expired_state" in resp.headers["location"]

    def test_state_for_nonexistent_client_redirects_with_reason(self, client):
        state = create_oauth_state_token({"client_id": 999999, "user_id": 1})
        resp = client.get(
            "/api/oauth/meta/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=unknown_client" in resp.headers["location"]

    def test_happy_path_single_page_with_instagram(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        _mock_chain(
            monkeypatch,
            pages=[{"id": "page-1", "name": "Bloom Page", "access_token": "page-token-1"}],
            ig_by_page={"page-1": "ig-1"},
        )

        resp = client.get(
            "/api/oauth/meta/callback",
            params={"code": "auth-code-123", "state": state},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 307)
        location = resp.headers["location"]
        assert "meta_connect=success" in location
        assert "facebook=1" in location
        assert "instagram=1" in location

        fb_account = db_session.query(Account).filter(Account.platform == "facebook", Account.client_id == c.id).one()
        assert fb_account.credentials["page_id"] == "page-1"
        assert fb_account.credentials["page_token"] == "page-token-1"
        assert fb_account.credentials["user_token"] == "long-lived-token"
        assert fb_account.is_active is True

        ig_account = db_session.query(Account).filter(Account.platform == "instagram", Account.client_id == c.id).one()
        assert ig_account.credentials["ig_user_id"] == "ig-1"
        assert ig_account.credentials["page_id"] == "page-1"
        assert ig_account.name == fb_account.name

    def test_page_with_no_linked_instagram_only_creates_facebook_account(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        _mock_chain(
            monkeypatch,
            pages=[{"id": "page-1", "name": "Bloom Page", "access_token": "page-token-1"}],
            ig_by_page={},  # no linked IG account
        )

        resp = client.get(
            "/api/oauth/meta/callback",
            params={"code": "auth-code-123", "state": state},
            follow_redirects=False,
        )
        location = resp.headers["location"]
        assert "meta_connect=success" in location
        assert "facebook=1" in location
        assert "instagram=0" in location

        assert db_session.query(Account).filter(Account.platform == "facebook", Account.client_id == c.id).count() == 1
        assert db_session.query(Account).filter(Account.platform == "instagram", Account.client_id == c.id).count() == 0

    def test_multiple_pages_connect_multiple_accounts(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        _mock_chain(
            monkeypatch,
            pages=[
                {"id": "page-1", "name": "Bloom Main", "access_token": "page-token-1"},
                {"id": "page-2", "name": "Bloom Side", "access_token": "page-token-2"},
            ],
            ig_by_page={"page-1": "ig-1"},  # only the first Page has a linked IG account
        )

        resp = client.get(
            "/api/oauth/meta/callback",
            params={"code": "auth-code-123", "state": state},
            follow_redirects=False,
        )
        location = resp.headers["location"]
        assert "meta_connect=success" in location
        assert "facebook=2" in location
        assert "instagram=1" in location

        fb_accounts = db_session.query(Account).filter(Account.platform == "facebook", Account.client_id == c.id).all()
        assert {a.credentials["page_id"] for a in fb_accounts} == {"page-1", "page-2"}
        assert len({a.name for a in fb_accounts}) == 2  # distinct names, one per Page

        ig_accounts = db_session.query(Account).filter(Account.platform == "instagram", Account.client_id == c.id).all()
        assert len(ig_accounts) == 1
        assert ig_accounts[0].credentials["page_id"] == "page-1"

    def test_no_pages_redirects_with_reason(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        _mock_chain(monkeypatch, pages=[])

        resp = client.get(
            "/api/oauth/meta/callback",
            params={"code": "auth-code-123", "state": state},
            follow_redirects=False,
        )
        assert "reason=no_pages" in resp.headers["location"]
        assert db_session.query(Account).filter(Account.platform == "facebook", Account.client_id == c.id).count() == 0

    def test_reconnecting_rotates_existing_accounts_instead_of_duplicating(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")

        state1 = create_oauth_state_token({"client_id": c.id, "user_id": 1})
        _mock_chain(
            monkeypatch,
            pages=[{"id": "page-1", "name": "Bloom Page", "access_token": "page-token-1"}],
            ig_by_page={"page-1": "ig-1"},
        )
        client.get("/api/oauth/meta/callback", params={"code": "first-code", "state": state1}, follow_redirects=False)

        state2 = create_oauth_state_token({"client_id": c.id, "user_id": 1})
        monkeypatch.setattr(
            dashboard_api.meta_publisher,
            "exchange_code_for_user_token",
            lambda code, redirect_uri: {"access_token": "rotated-short-lived-token", "expires_in": 3600},
        )
        monkeypatch.setattr(
            dashboard_api.meta_publisher,
            "exchange_long_lived_token",
            lambda short_lived_token: {"access_token": "rotated-long-lived-token", "expires_at": "2026-06-01T00:00:00+00:00"},
        )
        monkeypatch.setattr(
            dashboard_api.meta_publisher,
            "list_pages",
            lambda user_token: [{"id": "page-1", "name": "Bloom Page", "access_token": "rotated-page-token"}],
        )
        monkeypatch.setattr(
            dashboard_api.meta_publisher, "get_instagram_business_account", lambda page_id, page_token: "ig-1"
        )
        client.get("/api/oauth/meta/callback", params={"code": "second-code", "state": state2}, follow_redirects=False)

        fb_accounts = db_session.query(Account).filter(Account.platform == "facebook", Account.client_id == c.id).all()
        assert len(fb_accounts) == 1
        assert fb_accounts[0].credentials["page_token"] == "rotated-page-token"
        assert fb_accounts[0].credentials["user_token"] == "rotated-long-lived-token"

        ig_accounts = db_session.query(Account).filter(Account.platform == "instagram", Account.client_id == c.id).all()
        assert len(ig_accounts) == 1
        assert ig_accounts[0].credentials["page_token"] == "rotated-page-token"

    def test_permanent_error_during_exchange_redirects_with_reason(self, client, db_session, monkeypatch):
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        def fake_exchange(code, redirect_uri):
            raise PermanentError("Graph API rejected the authorization code")

        monkeypatch.setattr(dashboard_api.meta_publisher, "exchange_code_for_user_token", fake_exchange)

        resp = client.get(
            "/api/oauth/meta/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=exchange_failed" in resp.headers["location"]
        assert db_session.query(Account).filter(Account.platform == "facebook", Account.client_id == c.id).count() == 0

    def test_transient_error_during_exchange_redirects_with_reason_and_no_partial_accounts(
        self, client, db_session, monkeypatch
    ):
        # A transient failure partway through a multi-Page loop (e.g. the
        # second Page's Instagram lookup hits a rate limit) must not leave
        # the first Page's Account committed — the whole exchange is one
        # transaction, rolled back on any PublishError.
        c = _make_client(db_session, name="Bloom Studio")
        state = create_oauth_state_token({"client_id": c.id, "user_id": 1})

        _mock_chain(
            monkeypatch,
            pages=[
                {"id": "page-1", "name": "Bloom Main", "access_token": "page-token-1"},
                {"id": "page-2", "name": "Bloom Side", "access_token": "page-token-2"},
            ],
        )

        calls = {"count": 0}

        def flaky_ig_lookup(page_id, page_token):
            calls["count"] += 1
            if calls["count"] == 2:
                raise TransientError("Graph API rate limited")
            return None

        monkeypatch.setattr(dashboard_api.meta_publisher, "get_instagram_business_account", flaky_ig_lookup)

        resp = client.get(
            "/api/oauth/meta/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert "reason=exchange_failed" in resp.headers["location"]
        assert db_session.query(Account).filter(Account.platform == "facebook", Account.client_id == c.id).count() == 0
