"""
Tests for app/publishers/meta.py (Phase 23 — OAuth foundation only, no
publish() yet): the OAuth chain helpers (code/token exchange, Page listing,
Instagram Business account lookup), Graph API error classification, and the
token_expires_within/refresh_stored_credentials proactive-refresh contract.
All HTTP is mocked with `responses` — nothing here talks to the real Graph
API.
"""

from datetime import datetime, timedelta, timezone

import pytest
import responses

from app.exceptions import PermanentError, TransientError
from app.publishers import meta as meta_publisher

APP_ID = "app-123"
APP_SECRET = "secret-456"


@pytest.fixture(autouse=True)
def app_credentials(monkeypatch):
    monkeypatch.setenv("META_APP_ID", APP_ID)
    monkeypatch.setenv("META_APP_SECRET", APP_SECRET)


CREDENTIALS = {
    "page_id": "page-1",
    "page_token": "old-page-token",
    "page_name": "Old Name",
    "user_token": "old-user-token",
    "user_token_expires_at": None,
}


class TestMissingAppCredentials:
    def test_exchange_code_requires_app_credentials(self, monkeypatch):
        monkeypatch.delenv("META_APP_ID", raising=False)
        monkeypatch.delenv("META_APP_SECRET", raising=False)
        with pytest.raises(PermanentError):
            meta_publisher.exchange_code_for_user_token("code", "https://example.com/callback")

    def test_exchange_long_lived_requires_app_credentials(self, monkeypatch):
        monkeypatch.delenv("META_APP_ID", raising=False)
        monkeypatch.delenv("META_APP_SECRET", raising=False)
        with pytest.raises(PermanentError):
            meta_publisher.exchange_long_lived_token("short-token")


class TestOAuthChain:
    @responses.activate
    def test_exchange_code_for_user_token(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/oauth/access_token",
            json={"access_token": "short-tok", "token_type": "bearer", "expires_in": 3600},
            status=200,
        )
        result = meta_publisher.exchange_code_for_user_token("auth-code", "https://example.com/callback")
        assert result == {"access_token": "short-tok", "expires_in": 3600}

    @responses.activate
    def test_exchange_long_lived_token(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/oauth/access_token",
            json={"access_token": "long-tok", "token_type": "bearer", "expires_in": 5184000},
            status=200,
        )
        result = meta_publisher.exchange_long_lived_token("short-tok")
        assert result["access_token"] == "long-tok"
        expiry = datetime.fromisoformat(result["expires_at"])
        assert expiry > datetime.now(timezone.utc) + timedelta(days=59)

    @responses.activate
    def test_list_pages(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/me/accounts",
            json={"data": [{"id": "page-1", "name": "My Page", "access_token": "page-tok"}]},
            status=200,
        )
        pages = meta_publisher.list_pages("long-tok")
        assert pages == [{"id": "page-1", "name": "My Page", "access_token": "page-tok"}]

    @responses.activate
    def test_get_instagram_business_account_present(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/page-1",
            json={"instagram_business_account": {"id": "ig-1"}},
            status=200,
        )
        assert meta_publisher.get_instagram_business_account("page-1", "page-tok") == "ig-1"

    @responses.activate
    def test_get_instagram_business_account_absent(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/page-1",
            json={},
            status=200,
        )
        assert meta_publisher.get_instagram_business_account("page-1", "page-tok") is None


class TestGraphErrorClassification:
    @responses.activate
    def test_http_500_is_transient(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/me/accounts",
            json={"error": {"message": "oops", "code": 2}},
            status=500,
        )
        with pytest.raises(TransientError):
            meta_publisher.list_pages("tok")

    @responses.activate
    def test_http_429_is_transient(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/me/accounts",
            json={"error": {"message": "rate limited", "code": 4}},
            status=429,
        )
        with pytest.raises(TransientError):
            meta_publisher.list_pages("tok")

    @responses.activate
    def test_rate_limit_error_code_is_transient(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/me/accounts",
            json={"error": {"message": "App rate limit reached", "code": 4}},
            status=400,
        )
        with pytest.raises(TransientError):
            meta_publisher.list_pages("tok")

    @responses.activate
    def test_invalid_token_error_is_permanent(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/me/accounts",
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )
        with pytest.raises(PermanentError):
            meta_publisher.list_pages("tok")

    @responses.activate
    def test_other_4xx_is_permanent(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/me/accounts",
            json={"error": {"message": "Bad request", "code": 100}},
            status=400,
        )
        with pytest.raises(PermanentError):
            meta_publisher.list_pages("tok")


class TestTokenExpiresWithin:
    def test_missing_expiry_needs_refresh(self):
        assert meta_publisher.token_expires_within({}, 3600) is True

    def test_unparseable_expiry_needs_refresh(self):
        assert meta_publisher.token_expires_within({"user_token_expires_at": "not-a-date"}, 3600) is True

    def test_far_future_expiry_does_not_need_refresh(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        assert meta_publisher.token_expires_within({"user_token_expires_at": expiry}, 7 * 24 * 3600) is False

    def test_near_expiry_needs_refresh(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        assert meta_publisher.token_expires_within({"user_token_expires_at": expiry}, 7 * 24 * 3600) is True

    def test_naive_expiry_treated_as_utc(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=3)).replace(tzinfo=None).isoformat()
        assert meta_publisher.token_expires_within({"user_token_expires_at": expiry}, 7 * 24 * 3600) is True


class TestRefreshStoredCredentials:
    @responses.activate
    def test_happy_path_updates_user_and_page_token(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/oauth/access_token",
            json={"access_token": "new-user-token", "expires_in": 5184000},
            status=200,
        )
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/me/accounts",
            json={"data": [{"id": "page-1", "name": "New Name", "access_token": "new-page-token"}]},
            status=200,
        )

        updated = meta_publisher.refresh_stored_credentials(CREDENTIALS)

        assert updated["user_token"] == "new-user-token"
        assert updated["page_token"] == "new-page-token"
        assert updated["page_name"] == "New Name"
        assert updated["page_id"] == "page-1"
        expiry = datetime.fromisoformat(updated["user_token_expires_at"])
        assert expiry > datetime.now(timezone.utc) + timedelta(days=59)

    def test_missing_user_token_is_permanent(self):
        with pytest.raises(PermanentError):
            meta_publisher.refresh_stored_credentials({"page_id": "page-1"})

    def test_missing_page_id_is_permanent(self):
        with pytest.raises(PermanentError):
            meta_publisher.refresh_stored_credentials({"user_token": "tok"})

    @responses.activate
    def test_page_no_longer_accessible_is_permanent(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/oauth/access_token",
            json={"access_token": "new-user-token", "expires_in": 5184000},
            status=200,
        )
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/me/accounts",
            json={"data": [{"id": "some-other-page", "name": "Other", "access_token": "tok"}]},
            status=200,
        )
        with pytest.raises(PermanentError):
            meta_publisher.refresh_stored_credentials(CREDENTIALS)

    @responses.activate
    def test_transient_error_during_exchange_propagates(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/oauth/access_token",
            json={"error": {"message": "temporary", "code": 2}},
            status=500,
        )
        with pytest.raises(TransientError):
            meta_publisher.refresh_stored_credentials(CREDENTIALS)

    @responses.activate
    def test_invalid_user_token_during_exchange_is_permanent(self):
        responses.add(
            responses.GET,
            f"{meta_publisher.GRAPH_API_BASE}/oauth/access_token",
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )
        with pytest.raises(PermanentError):
            meta_publisher.refresh_stored_credentials(CREDENTIALS)
