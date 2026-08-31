"""
Tests for app.tasks.dispatch_due_jobs (Phase 5, timezone-aware since Phase
20), called directly (no Celery worker/broker) against the throwaway
SQLite DB from tests/conftest.py. publish_job.delay is monkeypatched so no
broker connection is needed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import tasks
from app.models import Job, JobStatus


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks.publish_job, "delay", lambda job_id: calls.append(job_id))
    return calls


def _make_scheduled_job(db_session, scheduled_at):
    job = Job(platform="twitter", payload={"text": "hi"}, status=JobStatus.SCHEDULED, scheduled_at=scheduled_at)
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


class TestDispatchDueJobs:
    def test_picks_up_due_job_with_aware_scheduled_at(self, db_session, dispatched):
        due = datetime.now(timezone.utc) - timedelta(minutes=1)
        job = _make_scheduled_job(db_session, due)

        tasks.dispatch_due_jobs()

        assert dispatched == [job.id]
        db_session.refresh(job)
        assert job.status == JobStatus.QUEUED

    def test_picks_up_due_job_with_naive_scheduled_at(self, db_session, dispatched):
        # Simulates a naive-but-UTC scheduled_at (SQLite round-trips every
        # datetime as naive regardless of what was written — see
        # app/tasks.py::_ensure_utc — and this is also what a pre-Phase-20
        # row would look like). Must still compare correctly against the
        # aware `now` dispatch_due_jobs uses now.
        due_naive = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)
        job = _make_scheduled_job(db_session, due_naive)

        tasks.dispatch_due_jobs()

        assert dispatched == [job.id]
        db_session.refresh(job)
        assert job.status == JobStatus.QUEUED

    def test_does_not_pick_up_future_job(self, db_session, dispatched):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        job = _make_scheduled_job(db_session, future)

        tasks.dispatch_due_jobs()

        assert dispatched == []
        db_session.refresh(job)
        assert job.status == JobStatus.SCHEDULED
