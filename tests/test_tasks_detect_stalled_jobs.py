"""
Tests for app.tasks.detect_stalled_jobs (Phase 14), called directly (no
Celery worker, no broker) against the throwaway SQLite DB from
tests/conftest.py. send_alert is monkeypatched so no network call happens.

Uses the default stall_threshold_minutes=30 / stall_realert_minutes=120
(see app/config.py) rather than monkeypatching settings, since Settings is
a frozen dataclass instance.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import tasks
from app.models import Job, JobStatus


@pytest.fixture
def alert(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "send_alert", lambda message: calls.append(message))
    return calls


def _make_job(db_session, status, updated_at, last_stall_alert_at=None):
    job = Job(platform="tiktok", payload={"video_path": "clip.mp4"}, status=status)
    db_session.add(job)
    db_session.commit()

    # Set explicitly after the initial insert so this bypasses the column's
    # onupdate=_utcnow default (which only fills in when the column wasn't
    # already given an explicit value in the flush) and actually backdates it.
    job.updated_at = updated_at
    job.last_stall_alert_at = last_stall_alert_at
    db_session.commit()
    db_session.refresh(job)
    return job


class TestDetectStalledJobs:
    def test_stalled_queued_job_triggers_alert(self, db_session, alert):
        stuck_since = datetime.now(timezone.utc) - timedelta(minutes=45)
        job = _make_job(db_session, JobStatus.QUEUED, stuck_since)

        tasks.detect_stalled_jobs()

        assert len(alert) == 1
        assert f"#{job.id}" in alert[0]
        assert "tiktok" in alert[0]
        db_session.refresh(job)
        assert job.last_stall_alert_at is not None

    def test_fresh_job_does_not_alert(self, db_session, alert):
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        _make_job(db_session, JobStatus.PROCESSING, recent)

        tasks.detect_stalled_jobs()

        assert alert == []

    def test_already_alerted_job_does_not_realert_within_window(self, db_session, alert):
        stuck_since = datetime.now(timezone.utc) - timedelta(minutes=45)
        alerted_recently = datetime.now(timezone.utc) - timedelta(minutes=10)
        _make_job(db_session, JobStatus.QUEUED, stuck_since, last_stall_alert_at=alerted_recently)

        tasks.detect_stalled_jobs()

        assert alert == []

    def test_realerts_once_the_realert_window_has_passed(self, db_session, alert):
        stuck_since = datetime.now(timezone.utc) - timedelta(minutes=45)
        alerted_long_ago = datetime.now(timezone.utc) - timedelta(minutes=150)
        job = _make_job(db_session, JobStatus.QUEUED, stuck_since, last_stall_alert_at=alerted_long_ago)

        tasks.detect_stalled_jobs()

        assert len(alert) == 1
        db_session.refresh(job)
        # SQLite round-trips last_stall_alert_at as naive; normalize before
        # comparing against the aware datetime built above.
        assert job.last_stall_alert_at.replace(tzinfo=timezone.utc) > alerted_long_ago

    def test_published_job_is_never_considered_stalled(self, db_session, alert):
        stuck_since = datetime.now(timezone.utc) - timedelta(minutes=45)
        _make_job(db_session, JobStatus.PUBLISHED, stuck_since)

        tasks.detect_stalled_jobs()

        assert alert == []
