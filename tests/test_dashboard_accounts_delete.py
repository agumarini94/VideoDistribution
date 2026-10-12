"""
Tests for Phase 36: DELETE /api/accounts/{id} (dashboard/api.py) — the
destructive "Remove" counterpart to disconnect_account (Phase 33), which
only deactivates.

Uses FastAPI's TestClient (real HTTP layer, incl. enforce_auth + cookies),
same reasoning as tests/test_dashboard_accounts_disconnect.py: the behavior
under test (ownership scoping, admin access, the pending-jobs block) is
middleware + route-level, not a pure helper.
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


def _make_admin_user(db_session, email="admin@test.com", password="password123") -> User:
    admin = User(
        email=email,
        hashed_password=hash_password(password),
        role="admin",
        client_id=None,
        is_approved=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


def _make_account(
    db_session,
    client_row,
    platform="youtube",
    name="Acme Co (self-service)",
    credentials=None,
    is_active=False,
):
    account = Account(
        platform=platform,
        name=name,
        credentials=credentials if credentials is not None else {},
        client_id=client_row.id if client_row else None,
        is_active=is_active,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


def _make_job(db_session, account, status, client_row=None):
    job = Job(
        platform=account.platform,
        payload={"title": "a job"},
        account_id=account.id,
        client_id=client_row.id if client_row else None,
        status=status,
        attempts=1,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def _login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _account_row(db_session, account_id):
    # The DELETE request runs against its own SessionLocal() (dashboard/
    # api.py's get_db dependency), not db_session — so db_session's
    # identity map can still hold the pre-delete instance. expire_all()
    # forces the next .get() to actually hit the database instead of
    # returning that stale cached object.
    db_session.expire_all()
    return db_session.get(Account, account_id)


class TestDeleteHappyPath:
    def test_client_user_can_delete_own_account_with_no_jobs(self, client, db_session):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        account = _make_account(db_session, acme)
        account_id = account.id
        _login(client, "user@acme.test", "password123")

        resp = client.delete(f"/api/accounts/{account_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"id": account_id, "deleted": True, "jobs_unlinked": 0}

        assert _account_row(db_session, account_id) is None

    def test_admin_basic_auth_can_delete_any_account(self, client, db_session):
        acme = _make_client(db_session)
        account = _make_account(db_session, acme)
        account_id = account.id

        resp = client.delete(f"/api/accounts/{account_id}", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        assert _account_row(db_session, account_id) is None

    def test_admin_user_session_can_delete_any_account(self, client, db_session):
        _make_admin_user(db_session)
        acme = _make_client(db_session)
        account = _make_account(db_session, acme)
        account_id = account.id

        _login(client, "admin@test.com", "password123")
        resp = client.delete(f"/api/accounts/{account_id}")
        assert resp.status_code == 200
        assert _account_row(db_session, account_id) is None


class TestFinishedJobsAreUnlinkedNotBlocked:
    @pytest.mark.parametrize("status", [JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.NEEDS_USER_ACTION])
    def test_finished_job_is_unlinked_and_account_still_deletes(self, client, db_session, status):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        account = _make_account(db_session, acme)
        account_id = account.id
        job = _make_job(db_session, account, status, acme)
        job_id = job.id
        _login(client, "user@acme.test", "password123")

        resp = client.delete(f"/api/accounts/{account_id}")
        assert resp.status_code == 200
        assert resp.json()["jobs_unlinked"] == 1

        assert _account_row(db_session, account_id) is None
        db_session.refresh(job)
        assert job.id == job_id
        assert job.account_id is None
        assert job.status == status
        assert job.payload == {"title": "a job"}


class TestPendingJobsBlockDeletion:
    @pytest.mark.parametrize("status", [JobStatus.SCHEDULED, JobStatus.QUEUED, JobStatus.PROCESSING])
    def test_pending_job_blocks_deletion_with_409(self, client, db_session, status):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        account = _make_account(db_session, acme)
        account_id = account.id
        _make_job(db_session, account, status, acme)
        _login(client, "user@acme.test", "password123")

        resp = client.delete(f"/api/accounts/{account_id}")
        assert resp.status_code == 409
        assert "pendiente" in resp.json()["detail"].lower()

        # Nothing changed: the account still exists, untouched.
        assert _account_row(db_session, account_id) is not None

    def test_pending_job_blocks_deletion_even_for_admin(self, client, db_session):
        acme = _make_client(db_session)
        account = _make_account(db_session, acme)
        account_id = account.id
        _make_job(db_session, account, JobStatus.QUEUED, acme)

        resp = client.delete(f"/api/accounts/{account_id}", auth=ADMIN_AUTH)
        assert resp.status_code == 409
        assert _account_row(db_session, account_id) is not None

    def test_mix_of_pending_and_finished_jobs_still_blocks(self, client, db_session):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        account = _make_account(db_session, acme)
        account_id = account.id
        _make_job(db_session, account, JobStatus.PUBLISHED, acme)
        _make_job(db_session, account, JobStatus.QUEUED, acme)
        _login(client, "user@acme.test", "password123")

        resp = client.delete(f"/api/accounts/{account_id}")
        assert resp.status_code == 409
        assert _account_row(db_session, account_id) is not None


class TestPermissions:
    def test_anonymous_unauthorized(self, client, db_session):
        acme = _make_client(db_session)
        account = _make_account(db_session, acme)

        resp = client.delete(f"/api/accounts/{account.id}")
        assert resp.status_code == 401
        assert _account_row(db_session, account.id) is not None

    def test_client_user_cannot_delete_another_clients_account(self, client, db_session):
        acme = _make_client(db_session, name="Acme Co")
        bloom = _make_client(db_session, name="Bloom Studio")
        _make_client_user(db_session, acme, email="acme@test.com")
        bloom_account = _make_account(db_session, bloom, name="Bloom Studio (self-service)")
        _login(client, "acme@test.com", "password123")

        resp = client.delete(f"/api/accounts/{bloom_account.id}")
        assert resp.status_code == 403
        assert _account_row(db_session, bloom_account.id) is not None

    def test_client_user_cannot_delete_an_unscoped_account(self, client, db_session):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        unscoped_account = _make_account(db_session, None, name="Legacy account")
        _login(client, "user@acme.test", "password123")

        resp = client.delete(f"/api/accounts/{unscoped_account.id}")
        assert resp.status_code == 403
        assert _account_row(db_session, unscoped_account.id) is not None

    def test_unknown_account_404(self, client, db_session):
        acme = _make_client(db_session)
        _make_client_user(db_session, acme)
        _login(client, "user@acme.test", "password123")

        resp = client.delete("/api/accounts/999999")
        assert resp.status_code == 404
