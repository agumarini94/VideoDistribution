"""
Tests for Phase 30: GET /api/analytics/summary (dashboard/api.py).

Uses FastAPI's TestClient (real HTTP layer, incl. enforce_auth) — like
tests/test_dashboard_auth.py and unlike the direct-function-call style of
tests/test_dashboard_media_staging.py — because the client-scoping behavior
under test lives in the auth middleware + route, not in a pure helper.

No network, no Celery: the endpoint only reads the Job table.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from dashboard import api as dashboard_api
from app.auth import hash_password
from app.models import Client, Job, JobStatus, User

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
    return user


def _add_job(db_session, *, platform, status, client_id=None, created_at=None) -> Job:
    job = Job(platform=platform, payload={"text": "x"}, status=status, client_id=client_id)
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    if created_at is not None:
        job.created_at = created_at
        db_session.commit()
    return job


def _login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


class TestAuth:
    def test_anonymous_is_401(self, client):
        assert client.get("/api/analytics/summary").status_code == 401

    def test_admin_basic_auth_ok(self, client):
        resp = client.get("/api/analytics/summary", auth=ADMIN_AUTH)
        assert resp.status_code == 200


class TestEmptyData:
    def test_shape_with_no_jobs(self, client):
        resp = client.get("/api/analytics/summary", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        body = resp.json()

        assert body["total"] == 0
        assert body["published"] == 0
        assert body["failed"] == 0
        assert body["success_rate"] == 0.0
        assert body["by_status"] == {s.value: 0 for s in JobStatus}
        assert body["by_platform"] == {}
        assert body["platform_breakdown"] == []

        # Time series is always exactly 30 zero-filled, ascending days.
        ts = body["time_series"]
        assert len(ts) == 30
        assert all(point["count"] == 0 for point in ts)
        dates = [point["date"] for point in ts]
        assert dates == sorted(dates)
        assert dates[-1] == datetime.now(timezone.utc).date().isoformat()
        assert dates[0] == (datetime.now(timezone.utc).date() - timedelta(days=29)).isoformat()


class TestGrouping:
    def test_counts_by_platform_and_status(self, client, db_session):
        # youtube: 3 published, 1 failed  |  twitter: 2 published  |  tiktok: 1 queued
        for _ in range(3):
            _add_job(db_session, platform="youtube", status=JobStatus.PUBLISHED)
        _add_job(db_session, platform="youtube", status=JobStatus.FAILED)
        for _ in range(2):
            _add_job(db_session, platform="twitter", status=JobStatus.PUBLISHED)
        _add_job(db_session, platform="tiktok", status=JobStatus.QUEUED)

        body = client.get("/api/analytics/summary", auth=ADMIN_AUTH).json()

        assert body["total"] == 7
        assert body["published"] == 5
        assert body["failed"] == 1
        # 5 / (5 + 1) rounded to 4 dp
        assert body["success_rate"] == round(5 / 6, 4)

        assert body["by_status"]["published"] == 5
        assert body["by_status"]["failed"] == 1
        assert body["by_status"]["queued"] == 1
        assert body["by_status"]["scheduled"] == 0

        assert body["by_platform"] == {"youtube": 4, "twitter": 2, "tiktok": 1}

        breakdown = {row["platform"]: row for row in body["platform_breakdown"]}
        assert [row["platform"] for row in body["platform_breakdown"]] == ["tiktok", "twitter", "youtube"]  # sorted
        assert breakdown["youtube"]["total"] == 4
        assert breakdown["youtube"]["published"] == 3
        assert breakdown["youtube"]["failed"] == 1
        assert breakdown["youtube"]["last_activity"] is not None
        assert breakdown["twitter"] == {
            "platform": "twitter",
            "total": 2,
            "published": 2,
            "failed": 0,
            "last_activity": breakdown["twitter"]["last_activity"],
        }
        assert breakdown["tiktok"]["published"] == 0
        assert breakdown["tiktok"]["failed"] == 0

    def test_success_rate_zero_when_no_terminal_jobs(self, client, db_session):
        _add_job(db_session, platform="youtube", status=JobStatus.QUEUED)
        _add_job(db_session, platform="youtube", status=JobStatus.SCHEDULED)

        body = client.get("/api/analytics/summary", auth=ADMIN_AUTH).json()
        assert body["published"] == 0
        assert body["failed"] == 0
        assert body["success_rate"] == 0.0

    def test_time_series_buckets_today_and_a_past_day(self, client, db_session):
        now = datetime.now(timezone.utc)
        for _ in range(4):
            _add_job(db_session, platform="youtube", status=JobStatus.PUBLISHED, created_at=now)
        _add_job(
            db_session,
            platform="twitter",
            status=JobStatus.PUBLISHED,
            created_at=now - timedelta(days=5),
        )
        # Outside the 30-day window — must not appear anywhere in the series.
        _add_job(
            db_session,
            platform="twitter",
            status=JobStatus.PUBLISHED,
            created_at=now - timedelta(days=45),
        )

        ts = client.get("/api/analytics/summary", auth=ADMIN_AUTH).json()["time_series"]
        by_date = {point["date"]: point["count"] for point in ts}

        assert len(ts) == 30
        assert by_date[now.date().isoformat()] == 4
        assert by_date[(now.date() - timedelta(days=5)).isoformat()] == 1
        # The 45-day-old job is excluded, so the series still sums to 5.
        assert sum(point["count"] for point in ts) == 5


class TestClientScoping:
    def _setup(self, db_session):
        mine = _make_client(db_session, name="Mine Co")
        other = _make_client(db_session, name="Other Co")
        _make_client_user(db_session, mine, email="me@mine.test", password="password123")

        _add_job(db_session, platform="youtube", status=JobStatus.PUBLISHED, client_id=mine.id)
        _add_job(db_session, platform="youtube", status=JobStatus.FAILED, client_id=mine.id)
        for _ in range(5):
            _add_job(db_session, platform="tiktok", status=JobStatus.PUBLISHED, client_id=other.id)
        # A job with no client at all — visible to admin's unscoped view only.
        _add_job(db_session, platform="twitter", status=JobStatus.PUBLISHED, client_id=None)
        return mine, other

    def test_client_user_sees_only_their_own_numbers(self, client, db_session):
        mine, other = self._setup(db_session)
        _login(client, "me@mine.test", "password123")

        body = client.get("/api/analytics/summary").json()
        assert body["total"] == 2
        assert body["by_platform"] == {"youtube": 2}
        assert body["published"] == 1
        assert body["failed"] == 1

    def test_client_user_403_requesting_another_client(self, client, db_session):
        mine, other = self._setup(db_session)
        _login(client, "me@mine.test", "password123")

        resp = client.get(f"/api/analytics/summary?client_id={other.id}")
        assert resp.status_code == 403

    def test_client_user_own_client_id_is_allowed(self, client, db_session):
        mine, other = self._setup(db_session)
        _login(client, "me@mine.test", "password123")

        resp = client.get(f"/api/analytics/summary?client_id={mine.id}")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_admin_unscoped_sees_everything(self, client, db_session):
        mine, other = self._setup(db_session)
        body = client.get("/api/analytics/summary", auth=ADMIN_AUTH).json()
        assert body["total"] == 8
        assert body["by_platform"] == {"youtube": 2, "tiktok": 5, "twitter": 1}

    def test_admin_can_filter_by_client_id(self, client, db_session):
        mine, other = self._setup(db_session)
        body = client.get(f"/api/analytics/summary?client_id={other.id}", auth=ADMIN_AUTH).json()
        assert body["total"] == 5
        assert body["by_platform"] == {"tiktok": 5}
