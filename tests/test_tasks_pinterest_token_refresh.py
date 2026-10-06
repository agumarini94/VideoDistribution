"""
Tests for Phase 29e's Pinterest wiring into app/tasks.py's reactive
TokenExpiredError -> refresh -> retry-once path in publish_job
(_handle_token_expired, Phase 21) and the proactive Beat refresh
(refresh_expiring_tokens, Phase 8). Modeled directly on
tests/test_tasks_facebook_token_refresh.py/test_tasks_twitter_token_refresh.py.
Called directly (no Celery worker/broker) against the throwaway SQLite DB
from tests/conftest.py; the pinterest publisher and its
refresh_stored_credentials are monkeypatched so no real HTTP happens.
"""

import pytest

from app import tasks
from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.models import Account, Job, JobStatus
from app.publishers import pinterest as pinterest_publisher

CREDENTIALS = {"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": None}
NEW_CREDENTIALS = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_at": "2026-12-01T00:00:00+00:00"}


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
    account = Account(platform="pinterest", name="Main account", credentials=credentials, is_active=is_active)
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


def _make_job(db_session, account_id):
    job = Job(
        platform="pinterest",
        payload={"board_id": "b1", "title": "t", "media_public_url": "https://x/a.png"},
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
            [TokenExpiredError("expired"), {"platform": "pinterest", "external_id": "pin-999", "board_id": "b1"}]
        )
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "pinterest", fake_publisher)
        monkeypatch.setattr(pinterest_publisher, "refresh_stored_credentials", lambda creds: NEW_CREDENTIALS)

        tasks.publish_job(job.id)

        assert len(calls) == 2
        assert calls[0] == CREDENTIALS
        assert calls[1] == NEW_CREDENTIALS

        db_session.refresh(job)
        assert job.status == JobStatus.PUBLISHED
        assert job.external_id == "pin-999"

        db_session.refresh(account)
        assert account.credentials == NEW_CREDENTIALS
        assert account.is_active is True

    def test_permanently_invalid_token_deactivates_and_deadletters(self, db_session, monkeypatch, alert, dead_lettered):
        account = _make_account(db_session, CREDENTIALS)
        job = _make_job(db_session, account.id)

        fake_publisher, calls = _sequenced_publisher([TokenExpiredError("expired")])
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "pinterest", fake_publisher)
        monkeypatch.setattr(
            pinterest_publisher,
            "refresh_stored_credentials",
            lambda creds: (_ for _ in ()).throw(PermanentError("refresh token revoked")),
        )

        tasks.publish_job(job.id)

        db_session.refresh(account)
        assert account.is_active is False
        assert len(alert) == 1
        assert "needs re-authorization" in alert[0]
        assert "Connect Pinterest" in alert[0]

        db_session.refresh(job)
        assert job.status == JobStatus.FAILED
        assert len(dead_lettered) == 1

    def test_transient_refresh_error_falls_back_to_normal_backoff(self, db_session, monkeypatch, fake_retry):
        account = _make_account(db_session, CREDENTIALS)
        job = _make_job(db_session, account.id)

        fake_publisher, calls = _sequenced_publisher([TokenExpiredError("expired")])
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "pinterest", fake_publisher)
        monkeypatch.setattr(
            pinterest_publisher,
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
            platform="pinterest",
            payload={"board_id": "b1", "title": "t", "media_public_url": "https://x/a.png"},
            account_id=None,
            status=JobStatus.QUEUED,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        fake_publisher, calls = _sequenced_publisher([TokenExpiredError("expired")])
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "pinterest", fake_publisher)

        with pytest.raises(_RetryCalled):
            tasks.publish_job(job.id)

        assert len(calls) == 1  # never retried inline: nowhere to persist a refresh


class TestProactiveRefresh:
    def test_refreshes_when_expiring_soon(self, db_session, monkeypatch):
        account = _make_account(db_session, CREDENTIALS)
        monkeypatch.setattr(pinterest_publisher, "token_expires_within", lambda creds, seconds: True)
        monkeypatch.setattr(pinterest_publisher, "refresh_stored_credentials", lambda creds: NEW_CREDENTIALS)

        tasks.refresh_expiring_tokens()

        db_session.refresh(account)
        assert account.credentials == NEW_CREDENTIALS
        assert account.is_active is True

    def test_skips_when_not_expiring_soon(self, db_session, monkeypatch):
        account = _make_account(db_session, CREDENTIALS)
        monkeypatch.setattr(pinterest_publisher, "token_expires_within", lambda creds, seconds: False)

        def _should_not_be_called(creds):
            raise AssertionError("refresh_stored_credentials should not be called")

        monkeypatch.setattr(pinterest_publisher, "refresh_stored_credentials", _should_not_be_called)

        tasks.refresh_expiring_tokens()

        db_session.refresh(account)
        assert account.credentials == CREDENTIALS

    def test_permanent_refresh_error_deactivates_and_alerts(self, db_session, monkeypatch, alert):
        account = _make_account(db_session, CREDENTIALS)
        monkeypatch.setattr(pinterest_publisher, "token_expires_within", lambda creds, seconds: True)
        monkeypatch.setattr(
            pinterest_publisher,
            "refresh_stored_credentials",
            lambda creds: (_ for _ in ()).throw(PermanentError("refresh token revoked")),
        )

        tasks.refresh_expiring_tokens()

        db_session.refresh(account)
        assert account.is_active is False
        assert len(alert) == 1
