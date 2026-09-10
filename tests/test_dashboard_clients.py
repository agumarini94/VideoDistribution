"""
Tests for Phase 32: the Clients management screen backend (dashboard/api.py) —
GET /api/clients aggregate counts, POST /api/clients/{id}/deactivate and
/reactivate, admin-only enforcement, and a deactivated workspace blocking
its client_user logins (and severing an existing session).

Uses FastAPI's TestClient (real HTTP layer, incl. enforce_auth + cookies),
same reasoning as tests/test_dashboard_auth.py / tests/test_dashboard_analytics.py:
the behavior under test is middleware + route-level, not a pure helper.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard import api as dashboard_api
from app.auth import hash_password
from app.models import Account, Client, Job, JobStatus, User

ADMIN_AUTH = (dashboard_api._DASHBOARD_USERNAME, dashboard_api._DASHBOARD_PASSWORD)


@pytest.fixture(autouse=True)
def _no_real_dispatch(monkeypatch):
    monkeypatch.setattr(dashboard_api.publish_job, "delay", lambda job_id: None)


@pytest.fixture
def client():
    return TestClient(dashboard_api.app)


def _make_client(db_session, name="Acme Co", kind="client", is_active=True) -> Client:
    c = Client(name=name, kind=kind, is_active=is_active)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


def _make_client_user(db_session, client_row, email="user@acme.test", password="password123", is_approved=True) -> User:
    user = User(
        email=email,
        hashed_password=hash_password(password),
        role="client_user",
        client_id=client_row.id if client_row else None,
        is_approved=is_approved,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _clients_by_name(resp):
    return {c["name"]: c for c in resp.json()}


class TestAggregateCounts:
    def test_counts_are_correct_per_client(self, client, db_session):
        acme = _make_client(db_session, name="Acme Co")
        bloom = _make_client(db_session, name="Bloom Studio")

        # Acme: 2 accounts (one inactive — still counted), 2 approved users
        # + 1 pending (pending NOT counted), 3 jobs.
        db_session.add_all(
            [
                Account(platform="twitter", name="Acme A", credentials={}, client_id=acme.id, is_active=True),
                Account(platform="youtube", name="Acme B", credentials={}, client_id=acme.id, is_active=False),
                Account(platform="tiktok", name="Bloom A", credentials={}, client_id=bloom.id, is_active=True),
            ]
        )
        _make_client_user(db_session, acme, email="a1@acme.test")
        _make_client_user(db_session, acme, email="a2@acme.test")
        _make_client_user(db_session, acme, email="pending@acme.test", is_approved=False)
        db_session.add_all(
            [
                Job(platform="twitter", payload={"text": "1"}, client_id=acme.id, status=JobStatus.QUEUED),
                Job(platform="twitter", payload={"text": "2"}, client_id=acme.id, status=JobStatus.PUBLISHED),
                Job(platform="twitter", payload={"text": "3"}, client_id=acme.id, status=JobStatus.FAILED),
                Job(platform="tiktok", payload={"text": "x"}, client_id=bloom.id, status=JobStatus.QUEUED),
                Job(platform="fake", payload={"text": "no client"}, client_id=None, status=JobStatus.QUEUED),
            ]
        )
        db_session.commit()

        resp = client.get("/api/clients", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        rows = _clients_by_name(resp)

        assert rows["Acme Co"]["account_count"] == 2
        assert rows["Acme Co"]["user_count"] == 2  # pending excluded
        assert rows["Acme Co"]["job_count"] == 3
        assert rows["Acme Co"]["is_active"] is True

        assert rows["Bloom Studio"]["account_count"] == 1
        assert rows["Bloom Studio"]["user_count"] == 0
        assert rows["Bloom Studio"]["job_count"] == 1

    def test_client_with_nothing_reports_zeroes(self, client, db_session):
        _make_client(db_session, name="Empty Co")
        resp = client.get("/api/clients", auth=ADMIN_AUTH)
        row = _clients_by_name(resp)["Empty Co"]
        assert (row["account_count"], row["user_count"], row["job_count"]) == (0, 0, 0)
        assert row["is_active"] is True

    def test_create_client_response_carries_new_fields(self, client, db_session):
        resp = client.post("/api/clients", json={"name": "Fresh Co", "kind": "individual"}, auth=ADMIN_AUTH)
        assert resp.status_code == 201
        body = resp.json()
        assert body["is_active"] is True
        assert body["account_count"] == 0
        assert body["user_count"] == 0
        assert body["job_count"] == 0


class TestDeactivateReactivate:
    def test_deactivate_then_reactivate_flips_flag(self, client, db_session):
        c = _make_client(db_session, name="Toggle Co")

        d = client.post(f"/api/clients/{c.id}/deactivate", auth=ADMIN_AUTH)
        assert d.status_code == 200
        assert d.json()["is_active"] is False
        db_session.refresh(c)
        assert c.is_active is False

        r = client.post(f"/api/clients/{c.id}/reactivate", auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert r.json()["is_active"] is True
        db_session.refresh(c)
        assert c.is_active is True

    def test_deactivate_does_not_touch_accounts_users_or_jobs(self, client, db_session):
        c = _make_client(db_session, name="Keep Co")
        acc = Account(platform="twitter", name="Keep A", credentials={}, client_id=c.id, is_active=True)
        db_session.add(acc)
        u = _make_client_user(db_session, c, email="keep@keep.test")
        job = Job(platform="twitter", payload={"text": "x"}, client_id=c.id, status=JobStatus.QUEUED)
        db_session.add(job)
        db_session.commit()

        client.post(f"/api/clients/{c.id}/deactivate", auth=ADMIN_AUTH)

        db_session.refresh(acc)
        db_session.refresh(u)
        db_session.refresh(job)
        assert acc.is_active is True
        assert db_session.get(User, u.id) is not None
        assert db_session.get(Job, job.id) is not None
        # And the counts still reflect the untouched rows.
        row = _clients_by_name(client.get("/api/clients", auth=ADMIN_AUTH))["Keep Co"]
        assert (row["account_count"], row["user_count"], row["job_count"]) == (1, 1, 1)
        assert row["is_active"] is False

    def test_deactivate_unknown_client_404(self, client, db_session):
        assert client.post("/api/clients/99999/deactivate", auth=ADMIN_AUTH).status_code == 404
        assert client.post("/api/clients/99999/reactivate", auth=ADMIN_AUTH).status_code == 404


class TestAdminOnly:
    def test_client_user_forbidden(self, client, db_session):
        c = _make_client(db_session, name="Scoped Co")
        _make_client_user(db_session, c, email="cu@scoped.test", password="password123")
        _login(client, "cu@scoped.test", "password123")

        assert client.post(f"/api/clients/{c.id}/deactivate").status_code == 403
        assert client.post(f"/api/clients/{c.id}/reactivate").status_code == 403
        assert client.get("/api/clients").status_code == 403

    def test_anonymous_unauthorized(self, client, db_session):
        c = _make_client(db_session, name="Anon Co")
        assert client.post(f"/api/clients/{c.id}/deactivate").status_code == 401
        assert client.post(f"/api/clients/{c.id}/reactivate").status_code == 401


class TestDeactivatedWorkspaceBlocksUsers:
    def test_login_blocked_with_clear_message(self, client, db_session):
        c = _make_client(db_session, name="Dead Co", is_active=False)
        _make_client_user(db_session, c, email="stuck@dead.test", password="password123")

        resp = _login(client, "stuck@dead.test", "password123")
        assert resp.status_code == 403
        assert "deactivated" in resp.json()["detail"].lower()

    def test_login_works_again_after_reactivation(self, client, db_session):
        c = _make_client(db_session, name="Revive Co", is_active=False)
        _make_client_user(db_session, c, email="back@revive.test", password="password123")

        assert _login(client, "back@revive.test", "password123").status_code == 403

        client.post(f"/api/clients/{c.id}/reactivate", auth=ADMIN_AUTH)
        assert _login(client, "back@revive.test", "password123").status_code == 200

    def test_existing_session_severed_on_deactivation(self, client, db_session):
        c = _make_client(db_session, name="Live Co")
        _make_client_user(db_session, c, email="live@live.test", password="password123")

        assert _login(client, "live@live.test", "password123").status_code == 200
        assert client.get("/api/jobs").status_code == 200

        client.post(f"/api/clients/{c.id}/deactivate", auth=ADMIN_AUTH)

        # Same TestClient (same session cookie) — now resolves to anonymous.
        me = client.get("/api/auth/me")
        assert me.json()["authenticated"] is False
        assert client.get("/api/jobs").status_code == 401

    def test_admin_session_unaffected_by_any_client_deactivation(self, client, db_session):
        # An admin User isn't scoped to a client, so deactivating one must
        # not touch their access.
        c = _make_client(db_session, name="Whatever Co")
        admin = User(
            email="admin@ops.test",
            hashed_password=hash_password("password123"),
            role="admin",
            client_id=None,
            is_approved=True,
        )
        db_session.add(admin)
        db_session.commit()

        assert _login(client, "admin@ops.test", "password123").status_code == 200
        client.post(f"/api/clients/{c.id}/deactivate", auth=ADMIN_AUTH)
        assert client.get("/api/clients").status_code == 200
