"""
Tests for Phase 25's Instagram wiring into app/tasks.py's reactive
TokenExpiredError -> refresh -> retry-once path in publish_job
(_handle_token_expired, Phase 21). Mirrors
tests/test_tasks_facebook_token_refresh.py: "instagram" was already
registered in _TOKEN_REFRESH_MODULES_BY_PLATFORM (pointing at
app/publishers/meta.py, Phase 23) for the proactive Beat refresh, but the
reactive path only becomes reachable once instagram has a real publish()
that can raise TokenExpiredError (Phase 25) — this is what these tests
confirm end-to-end. Called directly (no Celery worker/broker) against the
throwaway SQLite DB from tests/conftest.py; the instagram publisher and
meta.py's refresh_stored_credentials are monkeypatched so no real HTTP
happens.
"""

import pytest

from app import tasks
from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.models import Account, Job, JobStatus
from app.publishers import meta as meta_publisher

CREDENTIALS = {
    "ig_user_id": "ig-1",
    "page_id": "page-1",
    "page_token": "old-page-tok",
    "user_token": "old-user-tok",
}
NEW_CREDENTIALS = {
    "ig_user_id": "ig-1",
    "page_id": "page-1",
    "page_token": "new-page-tok",
    "user_token": "new-user-tok",
    "user_token_expires_at": None,
}


class _RetryCalled(Exception):
    pass


@pytest.fixture
def alert(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "send_alert", lambda message: calls.append(message))
    return calls


@pytest.fixture
def dead_lettered(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks.handle_dead_letter, "apply_async", lambda args: calls.append(args))
    return calls


@pytest.fixture
def fake_retry(monkeypatch):
    calls = []

    def _retry(exc=None, countdown=None, **kwargs):
        calls.append(exc)
        raise _RetryCalled()

    monkeypatch.setattr(tasks.publish_job, "retry", _retry)
    return calls


def _make_account(db_session, credentials, is_active=True):
    account = Account(platform="instagram", name="Main Page", credentials=credentials, is_active=is_active)
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


def _make_job(db_session, account_id):
    job = Job(
        platform="instagram",
        payload={"media_public_url": "https://pub-example.r2.dev/photo.jpg"},
        account_id=account_id,
        status=JobStatus.QUEUED,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def _sequenced_publisher(outcomes):
    calls = []

    def fake(platform, payload, account_credentials):
        calls.append(account_credentials)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return fake, calls


class TestTokenExpiredRetry:
    def test_refresh_then_retry_succeeds(self, db_session, monkeypatch):
        account = _make_account(db_session, CREDENTIALS)
        job = _make_job(db_session, account.id)

        fake_publisher, calls = _sequenced_publisher(
            [TokenExpiredError("expired"), {"platform": "instagram", "external_id": "ig-media-999"}]
        )
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "instagram", fake_publisher)
        monkeypatch.setattr(meta_publisher, "refresh_stored_credentials", lambda creds: NEW_CREDENTIALS)

        tasks.publish_job(job.id)

        assert len(calls) == 2
        assert calls[0] == CREDENTIALS
        assert calls[1] == NEW_CREDENTIALS

        db_session.refresh(job)
        assert job.status == JobStatus.PUBLISHED
        assert job.external_id == "ig-media-999"

        db_session.refresh(account)
        assert account.credentials == NEW_CREDENTIALS
        assert account.is_active is True

    def test_permanently_invalid_token_deactivates_and_deadletters(self, db_session, monkeypatch, alert, dead_lettered):
        account = _make_account(db_session, CREDENTIALS)
        job = _make_job(db_session, account.id)

        fake_publisher, calls = _sequenced_publisher([TokenExpiredError("expired")])
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "instagram", fake_publisher)
        monkeypatch.setattr(
            meta_publisher,
            "refresh_stored_credentials",
            lambda creds: (_ for _ in ()).throw(PermanentError("invalid OAuth access token")),
        )

        tasks.publish_job(job.id)

        db_session.refresh(account)
        assert account.is_active is False
        assert len(alert) == 1
        assert "needs re-authorization" in alert[0]
        assert "authorize_meta" in alert[0]

        db_session.refresh(job)
        assert job.status == JobStatus.FAILED
        assert len(dead_lettered) == 1

    def test_transient_refresh_error_falls_back_to_normal_backoff(self, db_session, monkeypatch, fake_retry):
        account = _make_account(db_session, CREDENTIALS)
        job = _make_job(db_session, account.id)

        fake_publisher, calls = _sequenced_publisher([TokenExpiredError("expired")])
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "instagram", fake_publisher)
        monkeypatch.setattr(
            meta_publisher,
            "refresh_stored_credentials",
            lambda creds: (_ for _ in ()).throw(TransientError("network blip")),
        )

        with pytest.raises(_RetryCalled):
            tasks.publish_job(job.id)

        assert len(fake_retry) == 1
        assert "expired" in str(fake_retry[0])

        db_session.refresh(job)
        assert job.error_message == "expired"

        db_session.refresh(account)
        assert account.is_active is True
        assert account.credentials == CREDENTIALS  # unchanged: refresh never succeeded

    def test_no_account_cannot_be_refreshed_falls_back_to_normal_backoff(self, db_session, monkeypatch, fake_retry):
        job = Job(
            platform="instagram",
            payload={"media_public_url": "https://pub-example.r2.dev/photo.jpg"},
            account_id=None,
            status=JobStatus.QUEUED,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        fake_publisher, calls = _sequenced_publisher([TokenExpiredError("expired")])
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "instagram", fake_publisher)

        with pytest.raises(_RetryCalled):
            tasks.publish_job(job.id)

        assert len(calls) == 1  # never retried inline: nowhere to persist a refresh
