"""
Tests for app.tasks.handle_tiktok_webhook_event, called directly (no Celery
worker, no broker) against the in-memory-per-test SQLite DB from
tests/conftest.py. send_alert is monkeypatched so no network call happens
and so tests can assert on whether/how it was called.
"""

import pytest

from app import tasks
from app.models import Job, JobStatus, WebhookEvent


@pytest.fixture
def alert(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "send_alert", lambda message: calls.append(message))
    return calls


def _make_job(db_session, status=JobStatus.PROCESSING, external_id="pub-123"):
    job = Job(platform="tiktok", payload={"video_path": "clip.mp4"}, status=status, external_id=external_id)
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def _make_event(db_session, event_type, publish_id="pub-123", content=None):
    event = WebhookEvent(
        platform="tiktok",
        event_type=event_type,
        publish_id=publish_id,
        raw_payload={"event": event_type, "content": content or {}},
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)
    return event


class TestFailureEvent:
    def test_marks_job_failed_and_alerts(self, db_session, alert):
        job = _make_job(db_session, status=JobStatus.PROCESSING)
        event = _make_event(
            db_session,
            "video.upload.failed",
            publish_id=job.external_id,
            content={"publish_id": job.external_id, "fail_reason": "video_format_check_failed"},
        )

        tasks.handle_tiktok_webhook_event(event.id)

        db_session.refresh(job)
        assert job.status == JobStatus.FAILED
        assert "video_format_check_failed" in job.error_message
        assert len(alert) == 1
        assert str(job.id) in alert[0]

    def test_failure_without_fail_reason_falls_back_to_event_type(self, db_session, alert):
        job = _make_job(db_session, status=JobStatus.PROCESSING)
        event = _make_event(db_session, "video.upload.failed", publish_id=job.external_id, content={})

        tasks.handle_tiktok_webhook_event(event.id)

        db_session.refresh(job)
        assert job.status == JobStatus.FAILED
        assert "video.upload.failed" in job.error_message


class TestSuccessEvent:
    def test_is_idempotent_when_already_published(self, db_session, alert):
        job = _make_job(db_session, status=JobStatus.PUBLISHED)
        event = _make_event(db_session, "video.publish.completed", publish_id=job.external_id)

        tasks.handle_tiktok_webhook_event(event.id)

        db_session.refresh(job)
        assert job.status == JobStatus.PUBLISHED
        assert alert == []

    def test_never_resurrects_a_failed_job(self, db_session, alert):
        job = _make_job(db_session, status=JobStatus.FAILED)
        event = _make_event(db_session, "video.publish.completed", publish_id=job.external_id)

        tasks.handle_tiktok_webhook_event(event.id)

        db_session.refresh(job)
        assert job.status == JobStatus.FAILED
        assert alert == []

    def test_transitions_processing_job_to_published(self, db_session, alert):
        job = _make_job(db_session, status=JobStatus.PROCESSING)
        event = _make_event(db_session, "video.publish.completed", publish_id=job.external_id)

        tasks.handle_tiktok_webhook_event(event.id)

        db_session.refresh(job)
        assert job.status == JobStatus.PUBLISHED


class TestNoOpCases:
    def test_unknown_publish_id_is_a_noop(self, db_session, alert):
        job = _make_job(db_session, status=JobStatus.PROCESSING, external_id="pub-123")
        event = _make_event(db_session, "video.publish.completed", publish_id="no-such-publish-id")

        tasks.handle_tiktok_webhook_event(event.id)

        db_session.refresh(job)
        assert job.status == JobStatus.PROCESSING  # untouched
        assert alert == []

    def test_missing_publish_id_is_a_noop(self, db_session, alert):
        event = _make_event(db_session, "video.publish.completed", publish_id=None)

        tasks.handle_tiktok_webhook_event(event.id)  # must not raise

        assert alert == []

    def test_unrecognized_event_type_is_a_noop(self, db_session, alert):
        job = _make_job(db_session, status=JobStatus.PROCESSING, external_id="pub-123")
        event = _make_event(db_session, "authorization.removed", publish_id=job.external_id)

        tasks.handle_tiktok_webhook_event(event.id)

        db_session.refresh(job)
        assert job.status == JobStatus.PROCESSING  # untouched
        assert alert == []

    def test_nonexistent_webhook_event_id_is_a_noop(self, db_session, alert):
        tasks.handle_tiktok_webhook_event(999999)  # must not raise
        assert alert == []
