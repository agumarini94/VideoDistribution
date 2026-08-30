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
