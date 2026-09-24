"""
Tests for Phase 33: POST /api/accounts/{id}/disconnect (dashboard/api.py) —
the self-service "Disconnect" counterpart to the Phase 29a-d
/api/oauth/*/start "Connect ..." buttons.

Uses FastAPI's TestClient (real HTTP layer, incl. enforce_auth + cookies),
same reasoning as tests/test_dashboard_clients.py: the behavior under test
(ownership scoping, admin exclusion) is middleware + route-level, not a
pure helper.
"""

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.models import Account, Client, Job, JobStatus, User
from dashboard import api as dashboard_api

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
        client_id=client_row.id if client_row else None,
        is_approved=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_account(db_session, client_row, platform="youtube", name="Acme Co (self-service)", credentials=None):
    account = Account(
        platform=platform,
        name=name,
        credentials=credentials if credentials is not None else {"token": "secret-token", "refresh_token": "secret-refresh"},
        client_id=client_row.id if client_row else None,
        is_active=True,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


def _login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


class TestDisconnectHappyPath:
    def test_disconnect_clears_credentials_and_deactivates(self, client, db_session):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        account = _make_account(db_session, acme)
        _login(client, "user@acme.test", "password123")

        resp = client.post(f"/api/accounts/{account.id}/disconnect")
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_active"] is False
        # AccountOut never serializes credentials in the first place — see
        # its own docstring — so the API response itself can't reveal
        # whether they were cleared; assert against the DB row directly.
        assert "credentials" not in body

        db_session.refresh(account)
        assert account.is_active is False
        assert account.credentials == {}

    def test_disconnect_response_has_no_credentials_field(self, client, db_session):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        account = _make_account(db_session, acme)
        _login(client, "user@acme.test", "password123")

        resp = client.post(f"/api/accounts/{account.id}/disconnect")
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {
            "id",
            "platform",
            "name",
            "is_active",
            "created_at",
            "client_id",
            "client_name",
        }


class TestOwnershipEnforcement:
    def test_client_user_cannot_disconnect_another_clients_account(self, db_session, client):
        acme = _make_client(db_session, name="Acme Co")
        bloom = _make_client(db_session, name="Bloom Studio")
        _make_client_user(db_session, acme, email="acme@test.com")
        bloom_account = _make_account(db_session, bloom, name="Bloom Studio (self-service)")
        _login(client, "acme@test.com", "password123")

        resp = client.post(f"/api/accounts/{bloom_account.id}/disconnect")
        assert resp.status_code == 403

        db_session.refresh(bloom_account)
        assert bloom_account.is_active is True
        assert bloom_account.credentials != {}

    def test_admin_basic_auth_cannot_disconnect(self, client, db_session):
        acme = _make_client(db_session)
        account = _make_account(db_session, acme)

        resp = client.post(f"/api/accounts/{account.id}/disconnect", auth=ADMIN_AUTH)
        assert resp.status_code == 403
        db_session.refresh(account)
        assert account.is_active is True

    def test_admin_user_session_cannot_disconnect(self, client, db_session):
        admin = User(
            email="admin@test.com",
            hashed_password=hash_password("password123"),
            role="admin",
            client_id=None,
            is_approved=True,
        )
        db_session.add(admin)
        db_session.commit()
        acme = _make_client(db_session)
        account = _make_account(db_session, acme)

        _login(client, "admin@test.com", "password123")
        resp = client.post(f"/api/accounts/{account.id}/disconnect")
        assert resp.status_code == 403
        db_session.refresh(account)
        assert account.is_active is True

    def test_anonymous_unauthorized(self, client, db_session):
        acme = _make_client(db_session)
        account = _make_account(db_session, acme)
        resp = client.post(f"/api/accounts/{account.id}/disconnect")
        assert resp.status_code == 401

    def test_unknown_account_404(self, client, db_session):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        _login(client, "user@acme.test", "password123")

        resp = client.post("/api/accounts/999999/disconnect")
        assert resp.status_code == 404

    def test_client_user_cannot_disconnect_an_unscoped_account(self, client, db_session):
        # An Account with client_id=None (created before Phase 26, or via a
        # CLI script without --account-scoping) belongs to nobody's
        # workspace — a client_user must not be able to "claim" it either.
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        unscoped_account = _make_account(db_session, None, name="Legacy account")
        _login(client, "user@acme.test", "password123")

        resp = client.post(f"/api/accounts/{unscoped_account.id}/disconnect")
        assert resp.status_code == 403


class TestReconnectAfterDisconnect:
    def test_reconnecting_upserts_the_same_row_in_place(self, client, db_session):
        """
        Mirrors what the OAuth callback routes do (upsert_account matches
        on platform+name) — reconnecting after a disconnect should rotate
        the same Account row back to active with fresh credentials, not
        leave it stranded or create a duplicate.
        """
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        account = _make_account(db_session, acme, platform="youtube", name="Acme Co (self-service)")
        _login(client, "user@acme.test", "password123")

        disconnect_resp = client.post(f"/api/accounts/{account.id}/disconnect")
        assert disconnect_resp.status_code == 200
        db_session.refresh(account)
        assert account.is_active is False
        assert account.credentials == {}

        from scripts.add_account import upsert_account

        reconnected, action = upsert_account(
            db_session,
            platform="youtube",
            name="Acme Co (self-service)",
            credentials={"token": "brand-new-token", "refresh_token": "brand-new-refresh"},
        )
        db_session.commit()

        assert action == "Updated"
        assert reconnected.id == account.id
        assert reconnected.is_active is True
        assert reconnected.credentials == {"token": "brand-new-token", "refresh_token": "brand-new-refresh"}

        list_resp = client.get("/api/accounts")
        assert list_resp.status_code == 200
        rows = list_resp.json()
        assert len(rows) == 1
        assert rows[0]["id"] == account.id
        assert rows[0]["is_active"] is True


class TestJobHistoryPreserved:
    def test_existing_jobs_keep_their_account_link_and_status_after_disconnect(self, client, db_session):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        account = _make_account(db_session, acme)
        job = Job(
            platform="youtube",
            payload={"title": "already published"},
            account_id=account.id,
            client_id=acme.id,
            status=JobStatus.PUBLISHED,
            attempts=1,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)
        _login(client, "user@acme.test", "password123")

        resp = client.post(f"/api/accounts/{account.id}/disconnect")
        assert resp.status_code == 200

        db_session.refresh(job)
        assert job.account_id == account.id
        assert job.status == JobStatus.PUBLISHED
        assert job.payload == {"title": "already published"}
