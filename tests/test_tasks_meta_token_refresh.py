"""
Tests for Phase 23's Meta (facebook/instagram) wiring into
app/tasks.py::refresh_expiring_tokens: the 7-day proactive refresh window,
and deactivate+alert on a permanently invalid stored user token. There is no
reactive TokenExpiredError->refresh->retry path to test here (unlike
Twitter's, see test_tasks_twitter_token_refresh.py) — app/publishers/meta.py
has no publish() yet to ever raise it. Called directly against the
throwaway SQLite DB from tests/conftest.py; app/publishers/meta.py's
refresh_stored_credentials is monkeypatched so no real HTTP happens.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import tasks
from app.exceptions import PermanentError, TransientError
from app.models import Account
from app.publishers import meta as meta_publisher

CREDENTIALS = {
    "page_id": "page-1",
    "page_token": "old-page-tok",
    "page_name": "Old Name",
    "user_token": "old-user-tok",
}
NEW_CREDENTIALS = {
    "page_id": "page-1",
    "page_token": "new-page-tok",
    "page_name": "New Name",
    "user_token": "new-user-tok",
    "user_token_expires_at": None,
}


@pytest.fixture
def alert(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "send_alert", lambda message: calls.append(message))
    return calls


def _make_account(db_session, platform, credentials, is_active=True, name="Main Page"):
    account = Account(platform=platform, name=name, credentials=credentials, is_active=is_active)
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


class TestRefreshExpiringTokensMetaWindow:
    @pytest.mark.parametrize("platform", ["facebook", "instagram"])
    def test_refreshes_within_the_7_day_window(self, db_session, monkeypatch, platform):
        soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        account = _make_account(db_session, platform, {**CREDENTIALS, "user_token_expires_at": soon})
        monkeypatch.setattr(meta_publisher, "refresh_stored_credentials", lambda creds: NEW_CREDENTIALS)

        tasks.refresh_expiring_tokens()

        db_session.refresh(account)
        assert account.credentials == NEW_CREDENTIALS

    @pytest.mark.parametrize("platform", ["facebook", "instagram"])
    def test_does_not_refresh_beyond_the_7_day_window(self, db_session, monkeypatch, platform):
        later = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        credentials = {**CREDENTIALS, "user_token_expires_at": later}
        account = _make_account(db_session, platform, credentials)

        called = []
        monkeypatch.setattr(
            meta_publisher, "refresh_stored_credentials", lambda creds: called.append(creds) or NEW_CREDENTIALS
        )

        tasks.refresh_expiring_tokens()

        assert called == []
        db_session.refresh(account)
        assert account.credentials == credentials

    def test_missing_expiry_is_treated_as_needing_refresh(self, db_session, monkeypatch):
        account = _make_account(db_session, "facebook", CREDENTIALS)  # no user_token_expires_at key at all
        monkeypatch.setattr(meta_publisher, "refresh_stored_credentials", lambda creds: NEW_CREDENTIALS)

        tasks.refresh_expiring_tokens()

        db_session.refresh(account)
        assert account.credentials == NEW_CREDENTIALS

    def test_permanently_invalid_token_deactivates_and_alerts(self, db_session, monkeypatch, alert):
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        account = _make_account(db_session, "facebook", {**CREDENTIALS, "user_token_expires_at": soon})
        monkeypatch.setattr(
            meta_publisher,
            "refresh_stored_credentials",
            lambda creds: (_ for _ in ()).throw(PermanentError("invalid OAuth access token")),
        )

        tasks.refresh_expiring_tokens()

        db_session.refresh(account)
        assert account.is_active is False
        assert len(alert) == 1
        assert "needs re-authorization" in alert[0]
        assert "authorize_meta" in alert[0]

    def test_transient_error_leaves_account_active_and_unchanged(self, db_session, monkeypatch):
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        credentials = {**CREDENTIALS, "user_token_expires_at": soon}
        account = _make_account(db_session, "instagram", credentials)
        monkeypatch.setattr(
            meta_publisher,
            "refresh_stored_credentials",
            lambda creds: (_ for _ in ()).throw(TransientError("network blip")),
        )

        tasks.refresh_expiring_tokens()

        db_session.refresh(account)
        assert account.is_active is True
        assert account.credentials == credentials

    def test_inactive_account_is_never_considered(self, db_session, monkeypatch):
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        _make_account(db_session, "facebook", {**CREDENTIALS, "user_token_expires_at": soon}, is_active=False)
        called = []
        monkeypatch.setattr(
            meta_publisher, "refresh_stored_credentials", lambda creds: called.append(creds) or NEW_CREDENTIALS
        )

        tasks.refresh_expiring_tokens()

        assert called == []
