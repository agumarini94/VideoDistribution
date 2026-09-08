"""
Tests for app/publishers/tiktok.py: error classification (missing
credentials, HTTP 429/500/400, and the Content Posting API's "200 with a
nested error.code" shape) and one happy-path chunked upload. All HTTP is
mocked with `responses` — nothing here talks to the real TikTok API.
"""

import pytest
import responses

from app.exceptions import PermanentError, TransientError
from app.publishers import tiktok as tiktok_publisher

CREDENTIALS = {"access_token": "tok-123"}


def _payload(video_path):
    return {"video_path": str(video_path)}


def _init_response(publish_id="pub-123", upload_url="https://upload.example.com/put", code="ok"):
    return {"data": {"publish_id": publish_id, "upload_url": upload_url}, "error": {"code": code}}


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake video bytes, well under the 5MB single-chunk threshold")
    return path


class TestMissingCredentials:
    def test_no_account_credentials_is_permanent(self, video_file):
        with pytest.raises(PermanentError):
            tiktok_publisher.publish("tiktok", _payload(video_file), None)

    def test_blank_access_token_is_permanent(self, video_file):
        with pytest.raises(PermanentError):
            tiktok_publisher.publish("tiktok", _payload(video_file), {"access_token": "   "})

    def test_missing_video_path_is_permanent(self):
        with pytest.raises(PermanentError):
            tiktok_publisher.publish("tiktok", {}, CREDENTIALS)

    def test_video_file_not_found_is_permanent(self, tmp_path):
        missing = tmp_path / "does-not-exist.mp4"
        with pytest.raises(PermanentError):
            tiktok_publisher.publish("tiktok", _payload(missing), CREDENTIALS)


class TestInitHttpErrorClassification:
    @responses.activate
    def test_http_429_is_transient(self, video_file):
        responses.add(responses.POST, tiktok_publisher._INBOX_INIT_URL, status=429)
        with pytest.raises(TransientError):
            tiktok_publisher.publish("tiktok", _payload(video_file), CREDENTIALS)

    @responses.activate
    def test_http_500_is_transient(self, video_file):
        responses.add(responses.POST, tiktok_publisher._INBOX_INIT_URL, status=500)
        with pytest.raises(TransientError):
            tiktok_publisher.publish("tiktok", _payload(video_file), CREDENTIALS)

    @responses.activate
    def test_http_400_is_permanent(self, video_file):
        responses.add(responses.POST, tiktok_publisher._INBOX_INIT_URL, status=400)
        with pytest.raises(PermanentError):
            tiktok_publisher.publish("tiktok", _payload(video_file), CREDENTIALS)

    @responses.activate
    def test_http_401_is_permanent(self, video_file):
        responses.add(responses.POST, tiktok_publisher._INBOX_INIT_URL, status=401)
        with pytest.raises(PermanentError):
            tiktok_publisher.publish("tiktok", _payload(video_file), CREDENTIALS)


class Test200WithNestedErrorCode:
    """
    TikTok's Content Posting API answers most logical errors with HTTP 200
    and puts the real status in a nested body["error"]["code"] — a
    different shape from a plain HTTP error status, handled by the same
    _raise_for_api_error but worth covering as its own code path.
    """

    @responses.activate
    def test_transient_error_code_is_transient(self, video_file):
        body = _init_response(code="rate_limit_exceeded")
        responses.add(responses.POST, tiktok_publisher._INBOX_INIT_URL, json=body, status=200)
        with pytest.raises(TransientError):
            tiktok_publisher.publish("tiktok", _payload(video_file), CREDENTIALS)

    @responses.activate
    def test_permanent_error_code_is_permanent(self, video_file):
        body = _init_response(code="invalid_param")
        responses.add(responses.POST, tiktok_publisher._INBOX_INIT_URL, json=body, status=200)
        with pytest.raises(PermanentError):
            tiktok_publisher.publish("tiktok", _payload(video_file), CREDENTIALS)


class TestHappyPath:
    @responses.activate
    def test_full_inbox_upload_flow(self, video_file):
        upload_url = "https://upload.example.com/put"
        responses.add(
            responses.POST,
            tiktok_publisher._INBOX_INIT_URL,
            json=_init_response(publish_id="pub-123", upload_url=upload_url),
            status=200,
        )
        responses.add(responses.PUT, upload_url, status=201)

        result = tiktok_publisher.publish("tiktok", _payload(video_file), CREDENTIALS)

        assert result == {"platform": "tiktok", "external_id": "pub-123"}
        assert len(responses.calls) == 2  # the init POST + exactly one PUT chunk


class TestWebOAuthFlow:
    """Phase 29c — build_authorization_url/exchange_code_for_credentials, the
    web-flow (browser redirect) counterpart to scripts/authorize_tiktok.py's
    local-server flow, used by dashboard/api.py's /api/oauth/tiktok/*
    routes. Modeled on
    tests/test_publisher_twitter.py::TestWebOAuthFlow."""

    def test_build_authorization_url_missing_client_key_raises(self, monkeypatch):
        monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
        with pytest.raises(PermanentError):
            tiktok_publisher.build_authorization_url("http://localhost/callback", "state123", "deadbeef")

    def test_build_authorization_url_happy_path(self, monkeypatch):
        monkeypatch.setenv("TIKTOK_CLIENT_KEY", "client-key")

        url = tiktok_publisher.build_authorization_url("http://localhost/callback", "state123", "deadbeef")

        assert url.startswith(tiktok_publisher.AUTHORIZE_URL + "?")
        assert "client_key=client-key" in url
        assert "state=state123" in url
        assert "code_challenge=deadbeef" in url
        assert "code_challenge_method=S256" in url
        assert "response_type=code" in url

    @responses.activate
    def test_exchange_code_for_credentials_happy_path(self, monkeypatch):
        monkeypatch.setenv("TIKTOK_CLIENT_KEY", "client-key")
        monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "client-secret")
        responses.add(
            responses.POST,
            tiktok_publisher.TOKEN_URL,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "open_id": "user-123",
                "scope": "user.info.basic,video.upload",
                "expires_in": 86400,
            },
            status=200,
        )

        creds = tiktok_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback", "verifier123")

        assert creds["access_token"] == "new-access"
        assert creds["refresh_token"] == "new-refresh"
        assert creds["open_id"] == "user-123"
        assert creds["expires_at"] is not None

        request = responses.calls[0].request
        assert "grant_type=authorization_code" in request.body
        assert "code=auth-code" in request.body
        assert "code_verifier=verifier123" in request.body

    def test_exchange_code_for_credentials_missing_app_credentials_is_permanent(self, monkeypatch):
        monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
        monkeypatch.delenv("TIKTOK_CLIENT_SECRET", raising=False)
        with pytest.raises(PermanentError):
            tiktok_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback", "verifier123")

    @responses.activate
    def test_exchange_code_for_credentials_wraps_token_endpoint_failure_as_permanent(self, monkeypatch):
        # A PKCE verifier/challenge mismatch normally surfaces as a
        # TransientError or PermanentError from exchange_authorization_code
        # depending on the error code — this one-shot interactive flow
        # normalizes either way to PermanentError. See the function's
        # docstring.
        monkeypatch.setenv("TIKTOK_CLIENT_KEY", "client-key")
        monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "client-secret")
        responses.add(
            responses.POST,
            tiktok_publisher.TOKEN_URL,
            json={"error": "invalid_grant", "error_description": "code_verifier does not match code_challenge"},
            status=400,
        )

        with pytest.raises(PermanentError):
            tiktok_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback", "verifier123")

    @responses.activate
    def test_exchange_code_for_credentials_wraps_transient_token_error_as_permanent(self, monkeypatch):
        # Even a transient-classified token-endpoint error (server_error)
        # normalizes to PermanentError here — unlike
        # exchange_authorization_code (used by the CLI script), which lets
        # transient/permanent distinguish for a caller that might retry.
        monkeypatch.setenv("TIKTOK_CLIENT_KEY", "client-key")
        monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "client-secret")
        responses.add(
            responses.POST,
            tiktok_publisher.TOKEN_URL,
            json={"error": "server_error", "error_description": "temporary failure"},
            status=500,
        )

        with pytest.raises(PermanentError):
            tiktok_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback", "verifier123")
