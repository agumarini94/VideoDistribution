"""
Tests for Phase 28: self-registration, login, the session-cookie auth
middleware, per-client scoping, and the admin approve/reject flow
(dashboard/api.py). Uses FastAPI's TestClient (real HTTP layer, including
enforce_auth and cookies) rather than calling route functions directly,
since the whole point of this phase is request-level behavior — unlike the
rest of this suite (e.g. tests/test_dashboard_media_staging.py), which
calls dashboard/api.py functions directly and bypasses the middleware
entirely.

publish_job.delay is monkeypatched (autouse) so job-creation tests never
need a real Celery broker, same pattern as
tests/test_dashboard_media_staging.py. DASHBOARD_USERNAME/PASSWORD and
SESSION_SECRET_KEY are set in tests/conftest.py before any import, so
dashboard/api.py's real (non-dev-bypass) auth path is exercised here.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard import api as dashboard_api
from app.auth import hash_password
from app.models import Client, Job, JobStatus, User

ADMIN_AUTH = (dashboard_api._DASHBOARD_USERNAME, dashboard_api._DASHBOARD_PASSWORD)


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


class TestPublicPaths:
    def test_static_shell_is_public(self, client):
        # The operator/client SPA moved from "/" to "/dashboard" when
        # Arscor's public marketing site (public_pages.py) took over the
        # root — see tests/test_public_pages.py for "/"/"/terms"/"/privacy".
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_me_is_public_and_reports_anonymous(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": False, "role": None, "client_id": None, "client_name": None, "email": None}

    def test_gated_api_route_401_without_www_authenticate(self, client):
        resp = client.get("/api/jobs")
        assert resp.status_code == 401
        # Deliberately no WWW-Authenticate on /api/* — see enforce_auth's
        # docstring: that header would trigger a browser's native Basic-Auth
        # popup and hijack the SPA's own Login screen.
        assert "www-authenticate" not in {k.lower() for k in resp.headers.keys()}

    def test_docs_401_with_www_authenticate(self, client):
        resp = client.get("/docs")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Basic"

    def test_basic_auth_still_grants_full_access(self, client):
        resp = client.get("/api/jobs", auth=ADMIN_AUTH)
        assert resp.status_code == 200


class TestRegister:
    def test_happy_path_creates_pending_client_user(self, client, db_session):
        resp = client.post(
            "/api/auth/register",
            json={"email": "New.User@Acme.test", "password": "password123", "client_name": "Acme Co"},
        )
        assert resp.status_code == 201

        user = db_session.query(User).filter(User.email == "new.user@acme.test").one()
        assert user.role == "client_user"
        assert user.is_approved is False
        assert user.client_id is None
        assert user.requested_client_name == "Acme Co"

    def test_duplicate_email_returns_identical_response_and_no_new_row(self, client, db_session):
        payload = {"email": "dup@acme.test", "password": "password123", "client_name": "Acme Co"}
        first = client.post("/api/auth/register", json=payload)

        second_payload = {"email": "dup@acme.test", "password": "different-password", "client_name": "Other Co"}
        second = client.post("/api/auth/register", json=second_payload)

        # Same status and body regardless of the email already being taken
        # — this IS the no-enumeration behavior (a duplicate attempt is a
        # silent no-op, not a distinguishable error).
        assert second.status_code == first.status_code == 201
        assert second.json() == first.json()
        assert db_session.query(User).filter(User.email == "dup@acme.test").count() == 1

    def test_rejects_short_password(self, client):
        resp = client.post(
            "/api/auth/register",
            json={"email": "a@b.test", "password": "short", "client_name": "Acme Co"},
        )
        assert resp.status_code == 400

    def test_rejects_missing_client_name(self, client):
        resp = client.post(
            "/api/auth/register",
            json={"email": "a@b.test", "password": "password123", "client_name": "   "},
        )
        assert resp.status_code == 400


class TestLogin:
    def test_happy_path_sets_cookie_and_returns_identity(self, client, db_session):
        c = _make_client(db_session)
        _make_client_user(db_session, c, email="user@acme.test", password="password123")

        resp = _login(client, "user@acme.test", "password123")

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "authenticated": True,
            "role": "client_user",
            "client_id": c.id,
            "client_name": c.name,
            "email": "user@acme.test",
        }
        assert dashboard_api._SESSION_COOKIE_NAME in resp.cookies

    def test_wrong_password_is_generic_401(self, client, db_session):
        c = _make_client(db_session)
        _make_client_user(db_session, c, email="user@acme.test", password="password123")

        resp = _login(client, "user@acme.test", "wrong-password")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid email or password."

    def test_unknown_email_same_message_as_wrong_password(self, client, db_session):
        c = _make_client(db_session)
        _make_client_user(db_session, c, email="user@acme.test", password="password123")

        resp = _login(client, "nobody-registered@acme.test", "whatever12345")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid email or password."

    def test_pending_account_gets_distinct_message(self, client, db_session):
        c = _make_client(db_session)
        _make_client_user(db_session, c, email="pending@acme.test", password="password123", is_approved=False)

        resp = _login(client, "pending@acme.test", "password123")
        assert resp.status_code == 403
        assert "pending" in resp.json()["detail"].lower()


class TestClientUserScoping:
    def _setup_two_clients(self, db_session):
        mine = _make_client(db_session, name="Mine Co")
        other = _make_client(db_session, name="Other Co")
        _make_client_user(db_session, mine, email="me@mine.test", password="password123")

        my_job = Job(platform="fake", payload={"text": "mine"}, client_id=mine.id, status=JobStatus.QUEUED)
        other_job = Job(platform="fake", payload={"text": "other"}, client_id=other.id, status=JobStatus.FAILED)
        db_session.add_all([my_job, other_job])
        db_session.commit()
        db_session.refresh(my_job)
        db_session.refresh(other_job)
        return mine, other, my_job, other_job

    def test_jobs_scoped_to_own_client_by_default(self, client, db_session):
        mine, other, my_job, other_job = self._setup_two_clients(db_session)
        _login(client, "me@mine.test", "password123")

        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        ids = {j["id"] for j in resp.json()}
        assert ids == {my_job.id}

    def test_jobs_403_when_requesting_another_clients_id(self, client, db_session):
        mine, other, my_job, other_job = self._setup_two_clients(db_session)
        _login(client, "me@mine.test", "password123")

        resp = client.get(f"/api/jobs?client_id={other.id}")
        assert resp.status_code == 403

    def test_stats_scoped_to_own_client(self, client, db_session):
        mine, other, my_job, other_job = self._setup_two_clients(db_session)
        _login(client, "me@mine.test", "password123")

        resp = client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_retry_403_on_another_clients_job(self, client, db_session):
        mine, other, my_job, other_job = self._setup_two_clients(db_session)
        _login(client, "me@mine.test", "password123")

        resp = client.post(f"/api/jobs/{other_job.id}/retry")
        assert resp.status_code == 403

    def test_retry_ok_on_own_job(self, client, db_session):
        mine, other, my_job, other_job = self._setup_two_clients(db_session)
        my_job.status = JobStatus.FAILED
        db_session.commit()
        _login(client, "me@mine.test", "password123")

        resp = client.post(f"/api/jobs/{my_job.id}/retry")
        assert resp.status_code == 200

    def test_create_job_403_for_explicit_other_client_id(self, client, db_session):
        mine, other, _, _ = self._setup_two_clients(db_session)
        _login(client, "me@mine.test", "password123")

        resp = client.post("/api/jobs", data={"platform": "twitter", "text": "hi", "client_id": str(other.id)})
        assert resp.status_code == 403

    def test_create_job_forces_own_client_id_when_omitted(self, client, db_session):
        mine, other, _, _ = self._setup_two_clients(db_session)
        _login(client, "me@mine.test", "password123")

        resp = client.post("/api/jobs", data={"platform": "twitter", "text": "hi"})
        assert resp.status_code == 201
        job = db_session.get(Job, resp.json()["id"])
        assert job.client_id == mine.id

    def test_create_job_403_using_another_clients_account(self, client, db_session):
        mine, other, _, _ = self._setup_two_clients(db_session)
        other_account = dashboard_api.Account(platform="twitter", name="Other's account", credentials={}, client_id=other.id, is_active=True)
        db_session.add(other_account)
        db_session.commit()
        db_session.refresh(other_account)
        _login(client, "me@mine.test", "password123")

        resp = client.post(
            "/api/jobs",
            data={"platform": "twitter", "text": "hi", "account_id": str(other_account.id)},
        )
        assert resp.status_code == 403

    def test_accounts_scoped_to_own_client(self, client, db_session):
        mine, other, _, _ = self._setup_two_clients(db_session)
        mine_account = dashboard_api.Account(platform="twitter", name="Mine", credentials={}, client_id=mine.id, is_active=True)
        other_account = dashboard_api.Account(platform="twitter", name="Other", credentials={}, client_id=other.id, is_active=True)
        db_session.add_all([mine_account, other_account])
        db_session.commit()
        _login(client, "me@mine.test", "password123")

        resp = client.get("/api/accounts")
        assert resp.status_code == 200
        names = {a["name"] for a in resp.json()}
        assert names == {"Mine"}

    def test_clients_endpoint_forbidden_for_client_user(self, client, db_session):
        mine, other, _, _ = self._setup_two_clients(db_session)
        _login(client, "me@mine.test", "password123")

        assert client.get("/api/clients").status_code == 403
        assert client.post("/api/clients", json={"name": "New", "kind": "client"}).status_code == 403

    def test_admin_routes_forbidden_for_client_user(self, client, db_session):
        mine, other, _, _ = self._setup_two_clients(db_session)
        _login(client, "me@mine.test", "password123")

        assert client.get("/api/admin/users/pending").status_code == 403
        assert client.get("/api/admin/users").status_code == 403
        assert client.post("/api/admin/users/1/approve", json={"client_id": mine.id}).status_code == 403
        assert client.post("/api/admin/users/1/reject").status_code == 403


class TestAdminFlow:
    def test_pending_list_and_approve(self, client, db_session):
        target_client = _make_client(db_session, name="Bloom Studio")
        pending = _make_client_user(
            db_session, None, email="pending@bloom.test", password="password123", is_approved=False
        )
        pending.requested_client_name = "Bloom Studio"
        db_session.commit()

        pending_resp = client.get("/api/admin/users/pending", auth=ADMIN_AUTH)
        assert pending_resp.status_code == 200
        assert [u["email"] for u in pending_resp.json()] == ["pending@bloom.test"]

        approve_resp = client.post(
            f"/api/admin/users/{pending.id}/approve",
            json={"client_id": target_client.id},
            auth=ADMIN_AUTH,
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["client_id"] == target_client.id

        db_session.refresh(pending)
        assert pending.is_approved is True
        assert pending.client_id == target_client.id

        approved_resp = client.get("/api/admin/users", auth=ADMIN_AUTH)
        assert [u["email"] for u in approved_resp.json()] == ["pending@bloom.test"]

        still_pending = client.get("/api/admin/users/pending", auth=ADMIN_AUTH)
        assert still_pending.json() == []

    def test_reject_deletes_pending_user(self, client, db_session):
        pending = _make_client_user(db_session, None, email="reject-me@bloom.test", is_approved=False)

        resp = client.post(f"/api/admin/users/{pending.id}/reject", auth=ADMIN_AUTH)
        assert resp.status_code == 204
        # The delete happened on a different SessionLocal() (opened inside
        # the request via Depends(get_db)). db_session.get() would consult
        # its stale identity map (raising ObjectDeletedError once expired,
        # since it still believes the row should exist) — a fresh query
        # sidesteps the identity map instead and just returns no rows.
        assert db_session.query(User).filter(User.id == pending.id).first() is None

    def test_reject_refuses_already_approved_user(self, client, db_session):
        c = _make_client(db_session)
        approved = _make_client_user(db_session, c, email="approved@acme.test", is_approved=True)

        resp = client.post(f"/api/admin/users/{approved.id}/reject", auth=ADMIN_AUTH)
        assert resp.status_code == 400
        assert db_session.get(User, approved.id) is not None

    def test_approve_requires_admin(self, client, db_session):
        pending = _make_client_user(db_session, None, email="x@acme.test", is_approved=False)
        resp = client.post(f"/api/admin/users/{pending.id}/approve", json={"client_id": 1})
        assert resp.status_code == 401
