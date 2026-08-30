"""
Tests for app/publishers/twitter.py: error classification (missing app-level
or per-account credentials, HTTP 429/500/400/401) via a mocked
POST https://api.twitter.com/2/tweets (see tweepy.client.BaseClient.request).
No real X API call is ever made.
"""

import pytest
import responses

from app.exceptions import PermanentError, TransientError
from app.publishers import twitter as twitter_publisher

_TWEETS_URL = "https://api.twitter.com/2/tweets"


@pytest.fixture(autouse=True)
def _app_credentials(monkeypatch):
    monkeypatch.setenv("X_API_KEY", "app-key")
    monkeypatch.setenv("X_API_SECRET", "app-secret")
    monkeypatch.delenv("X_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("X_ACCESS_TOKEN_SECRET", raising=False)


ACCOUNT_CREDENTIALS = {"access_token": "acc-token", "access_token_secret": "acc-secret"}


class TestMissingCredentials:
    def test_missing_app_credentials_is_permanent(self, monkeypatch):
        monkeypatch.delenv("X_API_KEY", raising=False)
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {"text": "hello"}, ACCOUNT_CREDENTIALS)

    def test_no_account_and_no_env_fallback_is_permanent(self):
        # account_credentials is None (job has no account_id) and the
        # X_ACCESS_TOKEN* env fallback is unset (see _app_credentials fixture).
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {"text": "hello"}, None)

    def test_account_credentials_missing_token_secret_is_permanent(self):
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {"text": "hello"}, {"access_token": "acc-token"})

    def test_missing_text_payload_is_permanent(self):
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {}, ACCOUNT_CREDENTIALS)


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
    def test_http_401_is_permanent(self):
        responses.add(responses.POST, _TWEETS_URL, status=401)
        with pytest.raises(PermanentError):
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
        monkeypatch.setenv("X_ACCESS_TOKEN", "env-token")
        monkeypatch.setenv("X_ACCESS_TOKEN_SECRET", "env-token-secret")
        responses.add(
            responses.POST,
            _TWEETS_URL,
            json={"data": {"id": "42", "text": "hello"}},
            status=201,
        )

        result = twitter_publisher.publish("twitter", {"text": "hello"}, None)

        assert result == {"platform": "twitter", "external_id": "42"}
