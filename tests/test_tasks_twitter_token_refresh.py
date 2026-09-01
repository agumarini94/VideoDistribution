"""
Tests for Phase 21's Twitter/X OAuth 2.0 token-refresh wiring in
app/tasks.py: the reactive TokenExpiredError -> refresh -> retry-once path
in publish_job, and refresh_expiring_tokens picking up twitter accounts
with the platform-specific 40-minute refresh window. Called directly (no
Celery worker/broker) against the throwaway SQLite DB from
tests/conftest.py; the twitter publisher and its refresh_stored_credentials
are monkeypatched so no real HTTP happens, and Celery's retry()/DLQ dispatch
are monkeypatched too, so nothing here needs a Redis broker.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import tasks
from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.models import Account, Job, JobStatus
from app.publishers import twitter as twitter_publisher

CREDENTIALS = {"client_id": "cid", "client_secret": "csecret", "access_token": "old-token", "refresh_token": "old-refresh"}
NEW_CREDENTIALS = {
    "client_id": "cid",
    "client_secret": "csecret",
    "access_token": "new-token",
    "refresh_token": "new-refresh",
    "expires_at": None,
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
    account = Account(platform="twitter", name="Main", credentials=credentials, is_active=is_active)
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


def _make_job(db_session, account_id):
    job = Job(platform="twitter", payload={"text": "hi"}, account_id=account_id, status=JobStatus.QUEUED)
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
            [TokenExpiredError("expired"), {"platform": "twitter", "external_id": "999"}]
        )
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "twitter", fake_publisher)
        monkeypatch.setattr(twitter_publisher, "refresh_stored_credentials", lambda creds: NEW_CREDENTIALS)

        tasks.publish_job(job.id)

        assert len(calls) == 2
        assert calls[0] == CREDENTIALS
        assert calls[1] == NEW_CREDENTIALS

        db_session.refresh(job)
        assert job.status == JobStatus.PUBLISHED
        assert job.external_id == "999"

        db_session.refresh(account)
        assert account.credentials == NEW_CREDENTIALS
        assert account.is_active is True

    def test_refresh_is_persisted_before_the_retried_publish_call(self, db_session, monkeypatch):
        # Rotation must be committed to the DB before the retried publish()
        # call happens, not just held in memory — verified by reading the
        # account back from a second, independent session mid-call.
        account = _make_account(db_session, CREDENTIALS)
        job = _make_job(db_session, account.id)

        from app.db import SessionLocal

        seen_during_retry = {}

        def fake(platform, payload, account_credentials):
            if account_credentials == CREDENTIALS:
                raise TokenExpiredError("expired")
            other_session = SessionLocal()
            try:
                seen_during_retry["credentials"] = other_session.get(Account, account.id).credentials
            finally:
                other_session.close()
            return {"platform": "twitter", "external_id": "1"}

        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "twitter", fake)
        monkeypatch.setattr(twitter_publisher, "refresh_stored_credentials", lambda creds: NEW_CREDENTIALS)

        tasks.publish_job(job.id)

        assert seen_during_retry["credentials"] == NEW_CREDENTIALS

    def test_permanently_invalid_refresh_token_deactivates_and_deadletters(
        self, db_session, monkeypatch, alert, dead_lettered
    ):
        account = _make_account(db_session, CREDENTIALS)
        job = _make_job(db_session, account.id)

        fake_publisher, calls = _sequenced_publisher([TokenExpiredError("expired")])
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "twitter", fake_publisher)
        monkeypatch.setattr(
            twitter_publisher,
            "refresh_stored_credentials",
            lambda creds: (_ for _ in ()).throw(PermanentError("refresh token revoked")),
        )

        tasks.publish_job(job.id)

        db_session.refresh(account)
        assert account.is_active is False
        assert len(alert) == 1
        assert "needs re-authorization" in alert[0]
        assert "add_account" in alert[0]

        db_session.refresh(job)
        assert job.status == JobStatus.FAILED
        assert len(dead_lettered) == 1

    def test_transient_refresh_error_falls_back_to_normal_backoff(self, db_session, monkeypatch, fake_retry):
        account = _make_account(db_session, CREDENTIALS)
        job = _make_job(db_session, account.id)

        fake_publisher, calls = _sequenced_publisher([TokenExpiredError("expired")])
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "twitter", fake_publisher)
        monkeypatch.setattr(
            twitter_publisher,
            "refresh_stored_credentials",
            lambda creds: (_ for _ in ()).throw(TransientError("network blip")),
        )

        with pytest.raises(_RetryCalled):
            tasks.publish_job(job.id)

        # The ORIGINAL TokenExpiredError drives the retry, not the refresh
        # attempt's own (less informative) transient error.
        assert len(fake_retry) == 1
        assert "expired" in str(fake_retry[0])

        db_session.refresh(job)
        assert job.error_message == "expired"

        db_session.refresh(account)
        assert account.is_active is True
        assert account.credentials == CREDENTIALS  # unchanged: refresh never succeeded

    def test_no_account_cannot_be_refreshed_falls_back_to_normal_backoff(self, db_session, monkeypatch, fake_retry):
        job = Job(platform="twitter", payload={"text": "hi"}, account_id=None, status=JobStatus.QUEUED)
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        fake_publisher, calls = _sequenced_publisher([TokenExpiredError("expired")])
        monkeypatch.setitem(tasks._PUBLISHERS_BY_PLATFORM, "twitter", fake_publisher)

        with pytest.raises(_RetryCalled):
            tasks.publish_job(job.id)

        assert len(calls) == 1  # never retried inline: nowhere to persist a refresh


class TestRefreshExpiringTokensTwitterWindow:
    def test_refreshes_within_the_40_minute_window(self, db_session, monkeypatch):
        soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        account = _make_account(db_session, {**CREDENTIALS, "expires_at": soon})
        monkeypatch.setattr(twitter_publisher, "refresh_stored_credentials", lambda creds: NEW_CREDENTIALS)

        tasks.refresh_expiring_tokens()

        db_session.refresh(account)
        assert account.credentials == NEW_CREDENTIALS

    def test_does_not_refresh_beyond_the_40_minute_window(self, db_session, monkeypatch):
        later = (datetime.now(timezone.utc) + timedelta(minutes=50)).isoformat()
        credentials = {**CREDENTIALS, "expires_at": later}
        account = _make_account(db_session, credentials)

        called = []
        monkeypatch.setattr(
            twitter_publisher, "refresh_stored_credentials", lambda creds: called.append(creds) or NEW_CREDENTIALS
        )

        tasks.refresh_expiring_tokens()

        assert called == []
        db_session.refresh(account)
        assert account.credentials == credentials
