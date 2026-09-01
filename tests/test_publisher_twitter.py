"""
Tests for app/publishers/twitter.py (Phase 21: OAuth 2.0 Bearer + API v2):
credential resolution, error classification (HTTP 429/500/400/401/403, and
the RFC 6750 401-with-WWW-Authenticate-invalid_token -> TokenExpiredError
distinction) via a mocked POST https://api.twitter.com/2/tweets, and the
OAuth 2.0 token-refresh helpers (token_expires_within,
refresh_stored_credentials) against a mocked
https://api.twitter.com/2/oauth2/token. No real X API call is ever made.

See tests/test_publisher_twitter_media.py for chunked media upload and
thread-posting coverage.
"""

from datetime import datetime, timedelta, timezone

import pytest
import responses

from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.publishers import twitter as twitter_publisher

_TWEETS_URL = "https://api.twitter.com/2/tweets"
_TOKEN_URL = "https://api.twitter.com/2/oauth2/token"

ACCOUNT_CREDENTIALS = {
    "client_id": "client-id",
    "client_secret": "client-secret",
    "access_token": "acc-token",
    "refresh_token": "refresh-token",
}


@pytest.fixture(autouse=True)
def _no_env_credentials(monkeypatch):
    for var in ("TWITTER_CLIENT_ID", "TWITTER_CLIENT_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


class TestMissingCredentials:
    @pytest.mark.parametrize("missing_field", ["client_id", "client_secret", "access_token", "refresh_token"])
    def test_missing_account_field_is_permanent(self, missing_field):
        creds = {k: v for k, v in ACCOUNT_CREDENTIALS.items() if k != missing_field}
        with pytest.raises(PermanentError, match=missing_field):
            twitter_publisher.publish("twitter", {"text": "hello"}, creds)

    def test_no_account_and_no_env_fallback_is_permanent(self):
        # account_credentials is None (job has no account_id) and the
        # TWITTER_* env fallback is unset (see _no_env_credentials fixture).
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {"text": "hello"}, None)

    def test_missing_text_payload_is_permanent(self):
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {}, ACCOUNT_CREDENTIALS)


class TestCharacterLimit:
    @responses.activate
    def test_exactly_280_chars_passes_validation(self):
        responses.add(
            responses.POST,
            _TWEETS_URL,
            json={"data": {"id": "1", "text": "x" * 280}},
            status=201,
        )

        result = twitter_publisher.publish("twitter", {"text": "x" * 280}, ACCOUNT_CREDENTIALS)

        assert result == {"platform": "twitter", "external_id": "1"}

    @responses.activate
    def test_281_chars_raises_permanent_error_without_sending(self):
        # No responses.add(...): if the code somehow reached an HTTP call
        # despite the validation error, this mock would raise a connection
        # error instead of letting a real request out.
        with pytest.raises(PermanentError, match="281"):
            twitter_publisher.publish("twitter", {"text": "x" * 281}, ACCOUNT_CREDENTIALS)


class TestHttpErrorClassification:
    @responses.activate
    def test_http_429_is_transient(self):
        responses.add(responses.POST, _TWEETS_URL, status=429)
        with pytest.raises(TransientError):
            twitter_publisher.publish("twitter", {"text": "hello"}, ACCOUNT_CREDENTIALS)

    @responses.activate
    def test_http_500_is_transient(self):
        responses.add(responses.POST, _TWEETS_URL, status=500)
        with pytest.raises(TransientError):
            twitter_publisher.publish("twitter", {"text": "hello"}, ACCOUNT_CREDENTIALS)

    @responses.activate
    def test_http_400_is_permanent(self):
        responses.add(responses.POST, _TWEETS_URL, status=400)
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {"text": "hello"}, ACCOUNT_CREDENTIALS)

    @responses.activate
    def test_http_401_without_invalid_token_header_is_permanent(self):
        # A 401 that isn't flagged as an expired/invalid Bearer token (e.g.
        # bad app credentials, revoked authorization) is not retryable.
        responses.add(responses.POST, _TWEETS_URL, status=401)
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {"text": "hello"}, ACCOUNT_CREDENTIALS)

    @responses.activate
    def test_http_401_with_invalid_token_header_is_token_expired(self):
        responses.add(
            responses.POST,
            _TWEETS_URL,
            status=401,
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="The access token expired"'},
        )
        with pytest.raises(TokenExpiredError):
            twitter_publisher.publish("twitter", {"text": "hello"}, ACCOUNT_CREDENTIALS)

    @responses.activate
    def test_http_403_is_permanent(self):
        responses.add(responses.POST, _TWEETS_URL, status=403)
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {"text": "hello"}, ACCOUNT_CREDENTIALS)


class TestHappyPath:
    @responses.activate
    def test_successful_tweet_returns_external_id(self):
        responses.add(
            responses.POST,
            _TWEETS_URL,
            json={"data": {"id": "1234567890", "text": "hello"}},
            status=201,
        )

        result = twitter_publisher.publish("twitter", {"text": "hello"}, ACCOUNT_CREDENTIALS)

        assert result == {"platform": "twitter", "external_id": "1234567890"}

    @responses.activate
    def test_env_fallback_used_when_no_account_credentials(self, monkeypatch):
        monkeypatch.setenv("TWITTER_CLIENT_ID", "env-client-id")
        monkeypatch.setenv("TWITTER_CLIENT_SECRET", "env-client-secret")
        monkeypatch.setenv("TWITTER_ACCESS_TOKEN", "env-token")
        monkeypatch.setenv("TWITTER_REFRESH_TOKEN", "env-refresh-token")
        responses.add(
            responses.POST,
            _TWEETS_URL,
            json={"data": {"id": "42", "text": "hello"}},
            status=201,
        )

        result = twitter_publisher.publish("twitter", {"text": "hello"}, None)

        assert result == {"platform": "twitter", "external_id": "42"}


class TestTokenExpiresWithin:
    def test_no_expires_at_needs_refresh(self):
        assert twitter_publisher.token_expires_within({}, 60) is True

    def test_unparseable_expires_at_needs_refresh(self):
        assert twitter_publisher.token_expires_within({"expires_at": "not-a-date"}, 60) is True

    def test_far_future_expiry_does_not_need_refresh(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        assert twitter_publisher.token_expires_within({"expires_at": future}, 60) is False

    def test_expiry_within_window_needs_refresh(self):
        soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        assert twitter_publisher.token_expires_within({"expires_at": soon}, 60) is True

    def test_past_expiry_needs_refresh(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert twitter_publisher.token_expires_within({"expires_at": past}, 60) is True


class TestRefreshStoredCredentials:
    @responses.activate
    def test_happy_path_rotates_both_tokens_and_persists_client_creds(self):
        responses.add(
            responses.POST,
            _TOKEN_URL,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200},
            status=200,
        )

        result = twitter_publisher.refresh_stored_credentials(ACCOUNT_CREDENTIALS)

        assert result["access_token"] == "new-access"
        assert result["refresh_token"] == "new-refresh"
        assert result["client_id"] == ACCOUNT_CREDENTIALS["client_id"]
        assert result["client_secret"] == ACCOUNT_CREDENTIALS["client_secret"]
        assert result["expires_at"] is not None

        request = responses.calls[0].request
        assert request.headers["Authorization"].startswith("Basic ")
        assert "grant_type=refresh_token" in request.body
        assert "refresh_token=refresh-token" in request.body

    @responses.activate
    def test_response_without_rotated_refresh_token_is_transient(self):
        # X's refresh tokens always rotate; a response missing one would
        # silently strand the account on the next refresh if reused as-is.
        responses.add(responses.POST, _TOKEN_URL, json={"access_token": "new-access"}, status=200)

        with pytest.raises(TransientError):
            twitter_publisher.refresh_stored_credentials(ACCOUNT_CREDENTIALS)

    @responses.activate
    def test_invalid_grant_is_permanent(self):
        responses.add(
            responses.POST,
            _TOKEN_URL,
            json={"error": "invalid_grant", "error_description": "refresh token expired or already used"},
            status=400,
        )

        with pytest.raises(PermanentError, match="invalid_grant"):
            twitter_publisher.refresh_stored_credentials(ACCOUNT_CREDENTIALS)

    @responses.activate
    def test_server_error_is_transient(self):
        responses.add(
            responses.POST,
            _TOKEN_URL,
            json={"error": "server_error", "error_description": "try again"},
            status=500,
        )

        with pytest.raises(TransientError):
            twitter_publisher.refresh_stored_credentials(ACCOUNT_CREDENTIALS)

    def test_missing_stored_field_is_permanent(self):
        creds = dict(ACCOUNT_CREDENTIALS)
        del creds["refresh_token"]
        with pytest.raises(PermanentError):
            twitter_publisher.refresh_stored_credentials(creds)
