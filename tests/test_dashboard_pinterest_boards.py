"""
Tests for GET /api/accounts/{account_id}/boards (Phase 29e) — the Composer's
Pinterest board picker. FastAPI TestClient, same reasoning as
tests/test_dashboard_accounts_disconnect.py: ownership scoping is
middleware + route-level behavior. app/publishers/pinterest.py::list_boards
is monkeypatched — no real Pinterest API call.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard import api as dashboard_api
from app.auth import hash_password
from app.exceptions import PermanentError, TransientError
from app.models import Account, Client, User

ADMIN_AUTH = (dashboard_api._DASHBOARD_USERNAME, dashboard_api._DASHBOARD_PASSWORD)


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


def _make_pinterest_account(db_session, client_row=None, name="Main account") -> Account:
    account = Account(
        platform="pinterest",
        name=name,
        credentials={"access_token": "tok"},
        is_active=True,
        client_id=client_row.id if client_row else None,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


class TestListAccountBoards:
    def test_anonymous_is_401(self, client, db_session):
        account = _make_pinterest_account(db_session)
        resp = client.get(f"/api/accounts/{account.id}/boards")
        assert resp.status_code == 401

    def test_unknown_account_is_404(self, client):
        resp = client.get("/api/accounts/999999/boards", auth=ADMIN_AUTH)
        assert resp.status_code == 404

    def test_non_pinterest_account_is_400(self, client, db_session):
        account = Account(platform="twitter", name="Main", credentials={}, is_active=True)
        db_session.add(account)
        db_session.commit()
        db_session.refresh(account)

        resp = client.get(f"/api/accounts/{account.id}/boards", auth=ADMIN_AUTH)
        assert resp.status_code == 400

    def test_client_user_sees_own_account_boards(self, client, db_session, monkeypatch):
        c = _make_client(db_session)
        _make_client_user(db_session, c)
        _login(client, "user@acme.test", "password123")
        account = _make_pinterest_account(db_session, client_row=c)

        monkeypatch.setattr(
            dashboard_api.pinterest_publisher,
            "list_boards",
            lambda credentials: [{"id": "b1", "name": "Board One"}, {"id": "b2", "name": "Board Two"}],
        )

        resp = client.get(f"/api/accounts/{account.id}/boards")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "b1", "name": "Board One"}, {"id": "b2", "name": "Board Two"}]

    def test_client_user_cross_tenant_is_403(self, client, db_session):
        c1 = _make_client(db_session, name="Client One")
        c2 = _make_client(db_session, name="Client Two")
        _make_client_user(db_session, c1)
        _login(client, "user@acme.test", "password123")
        other_account = _make_pinterest_account(db_session, client_row=c2)

        resp = client.get(f"/api/accounts/{other_account.id}/boards")
        assert resp.status_code == 403

    def test_client_user_cannot_see_unscoped_account(self, client, db_session):
        c = _make_client(db_session)
        _make_client_user(db_session, c)
        _login(client, "user@acme.test", "password123")
        unscoped_account = _make_pinterest_account(db_session, client_row=None)

        resp = client.get(f"/api/accounts/{unscoped_account.id}/boards")
        assert resp.status_code == 403

    def test_admin_can_see_any_account_boards(self, client, db_session, monkeypatch):
        c = _make_client(db_session)
        account = _make_pinterest_account(db_session, client_row=c)
        monkeypatch.setattr(dashboard_api.pinterest_publisher, "list_boards", lambda credentials: [{"id": "b1", "name": "Board"}])

        resp = client.get(f"/api/accounts/{account.id}/boards", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        assert resp.json() == [{"id": "b1", "name": "Board"}]

    def test_permanent_error_from_pinterest_is_502(self, client, db_session, monkeypatch):
        account = _make_pinterest_account(db_session)

        def _raise(credentials):
            raise PermanentError("invalid token")

        monkeypatch.setattr(dashboard_api.pinterest_publisher, "list_boards", _raise)

        resp = client.get(f"/api/accounts/{account.id}/boards", auth=ADMIN_AUTH)
        assert resp.status_code == 502

    def test_transient_error_from_pinterest_is_503(self, client, db_session, monkeypatch):
        account = _make_pinterest_account(db_session)

        def _raise(credentials):
            raise TransientError("rate limited")

        monkeypatch.setattr(dashboard_api.pinterest_publisher, "list_boards", _raise)

        resp = client.get(f"/api/accounts/{account.id}/boards", auth=ADMIN_AUTH)
        assert resp.status_code == 503
