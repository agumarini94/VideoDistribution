"""
Tests for app/publishers/facebook.py (Phase 24): credential resolution, the
three payload shapes (text/photo/video), Graph error classification
(including code=190 -> TokenExpiredError, distinct from meta.py's own
default PermanentError), and the resumable video upload's
interrupted/offset-resume behavior. All HTTP is mocked with `responses` —
nothing here talks to the real Graph API.
"""

import pytest
import responses
import requests

from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.publishers import facebook as facebook_publisher
from app.publishers import meta as meta_publisher

PAGE_ID = "page-1"
APP_ID = "app-999"

CREDENTIALS = {"page_id": PAGE_ID, "page_token": "page-tok", "page_name": "Main Page"}

_FEED_URL = f"{meta_publisher.GRAPH_API_BASE}/{PAGE_ID}/feed"
_PHOTOS_URL = f"{meta_publisher.GRAPH_API_BASE}/{PAGE_ID}/photos"
_VIDEOS_URL = f"{meta_publisher.GRAPH_API_BASE}/{PAGE_ID}/videos"
_UPLOADS_URL = f"{meta_publisher.GRAPH_API_BASE}/{APP_ID}/uploads"


@pytest.fixture(autouse=True)
def app_id(monkeypatch):
    monkeypatch.setenv("META_APP_ID", APP_ID)


def _upload_binary_url(session_id: str) -> str:
    return f"{meta_publisher.GRAPH_API_BASE}/upload:{session_id}"


class TestCredentialResolution:
    def test_no_account_credentials_is_permanent(self):
        with pytest.raises(PermanentError, match="requires an Account"):
            facebook_publisher.publish("facebook", {"text": "hi"}, None)

    def test_missing_fields_are_permanent(self):
        with pytest.raises(PermanentError, match="page_token"):
            facebook_publisher.publish("facebook", {"text": "hi"}, {"page_id": PAGE_ID})

    def test_empty_payload_is_permanent(self):
        with pytest.raises(PermanentError, match="text.*media_paths|media_paths.*text"):
            facebook_publisher.publish("facebook", {}, CREDENTIALS)


class TestTextPost:
    @responses.activate
    def test_happy_path(self):
        responses.add(responses.POST, _FEED_URL, json={"id": "page-1_123"}, status=200)

        result = facebook_publisher.publish("facebook", {"text": "hello world"}, CREDENTIALS)

        assert result == {"platform": "facebook", "external_id": "page-1_123"}
        sent = responses.calls[0].request.body
        assert "message=hello" in sent or "hello" in sent

    def test_missing_text_and_media_is_permanent(self):
        with pytest.raises(PermanentError):
            facebook_publisher.publish("facebook", {"text": ""}, CREDENTIALS)

    @responses.activate
    def test_missing_response_id_is_permanent(self):
        responses.add(responses.POST, _FEED_URL, json={}, status=200)
        with pytest.raises(PermanentError, match="missing id"):
            facebook_publisher.publish("facebook", {"text": "hi"}, CREDENTIALS)


class TestPhotoPost:
    @responses.activate
    def test_happy_path_with_caption(self, tmp_path):
        path = tmp_path / "photo.png"
        path.write_bytes(b"\x89PNG fake bytes")
        responses.add(responses.POST, _PHOTOS_URL, json={"id": "photo-1", "post_id": "page-1_456"}, status=200)

        result = facebook_publisher.publish("facebook", {"text": "look at this", "media_paths": [str(path)]}, CREDENTIALS)

        assert result == {"platform": "facebook", "external_id": "page-1_456"}

    @responses.activate
    def test_happy_path_without_caption(self, tmp_path):
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"\xff\xd8\xff fake jpeg")
        responses.add(responses.POST, _PHOTOS_URL, json={"id": "photo-2"}, status=200)

        result = facebook_publisher.publish("facebook", {"media_paths": [str(path)]}, CREDENTIALS)

        assert result == {"platform": "facebook", "external_id": "photo-2"}

    def test_missing_file_is_permanent(self):
        with pytest.raises(PermanentError, match="not found"):
            facebook_publisher.publish("facebook", {"media_paths": ["/nope/missing.png"]}, CREDENTIALS)

    def test_more_than_one_media_path_is_permanent(self, tmp_path):
        p1 = tmp_path / "a.png"
        p2 = tmp_path / "b.png"
        p1.write_bytes(b"\x89PNG a")
        p2.write_bytes(b"\x89PNG b")
        with pytest.raises(PermanentError, match="exactly one"):
            facebook_publisher.publish("facebook", {"media_paths": [str(p1), str(p2)]}, CREDENTIALS)


class TestVideoPost:
    @responses.activate
    def test_happy_path(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"0123456789ABCDEF")
        session_id = "SESSION123"
        upload_url = _upload_binary_url(session_id)

        responses.add(responses.POST, _UPLOADS_URL, json={"id": f"upload:{session_id}"}, status=200)
        responses.add(responses.POST, upload_url, json={"h": "handle-1"}, status=200)
        responses.add(responses.POST, _VIDEOS_URL, json={"id": "video-1"}, status=200)

        result = facebook_publisher.publish(
            "facebook", {"text": "a description", "title": "My video", "media_paths": [str(path)]}, CREDENTIALS
        )

        assert result == {"platform": "facebook", "external_id": "video-1"}

        upload_call = next(c for c in responses.calls if c.request.url == upload_url)
        assert upload_call.request.headers["Authorization"] == "OAuth page-tok"
        assert upload_call.request.headers["file_offset"] == "0"
        assert upload_call.request.body == path.read_bytes()

        publish_call = responses.calls[-1]
        assert "description=a" in publish_call.request.body or "description" in publish_call.request.body

    @responses.activate
    def test_interrupted_upload_resumes_from_offset(self, tmp_path):
        path = tmp_path / "clip.mp4"
        content = b"0123456789ABCDEF"
        path.write_bytes(content)
        session_id = "SESSION456"
        upload_url = _upload_binary_url(session_id)

        responses.add(responses.POST, _UPLOADS_URL, json={"id": f"upload:{session_id}"}, status=200)
        # First attempt is interrupted mid-flight (a network error).
        responses.add(responses.POST, upload_url, body=requests.exceptions.ConnectionError("connection dropped"))
        # Facebook reports how many bytes it actually received.
        responses.add(responses.GET, upload_url, json={"file_offset": 6}, status=200)
        # The retried POST succeeds, resuming from byte 6.
        responses.add(responses.POST, upload_url, json={"h": "handle-resumed"}, status=200)
        responses.add(responses.POST, _VIDEOS_URL, json={"id": "video-2"}, status=200)

        result = facebook_publisher.publish("facebook", {"text": "resumed", "media_paths": [str(path)]}, CREDENTIALS)

        assert result == {"platform": "facebook", "external_id": "video-2"}

        upload_posts = [c for c in responses.calls if c.request.method == "POST" and c.request.url == upload_url]
        assert len(upload_posts) == 2
        assert upload_posts[0].request.headers["file_offset"] == "0"
        assert upload_posts[1].request.headers["file_offset"] == "6"
        assert upload_posts[1].request.body == content[6:]

        offset_gets = [c for c in responses.calls if c.request.method == "GET" and c.request.url.startswith(upload_url)]
        assert len(offset_gets) == 1

    @responses.activate
    def test_upload_gives_up_after_max_attempts(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"0123456789ABCDEF")
        session_id = "SESSION789"
        upload_url = _upload_binary_url(session_id)

        responses.add(responses.POST, _UPLOADS_URL, json={"id": f"upload:{session_id}"}, status=200)
        for _ in range(facebook_publisher._MAX_UPLOAD_ATTEMPTS):
            responses.add(responses.POST, upload_url, body=requests.exceptions.ConnectionError("still down"))
        for _ in range(facebook_publisher._MAX_UPLOAD_ATTEMPTS - 1):
            responses.add(responses.GET, upload_url, json={"file_offset": 0}, status=200)

        with pytest.raises(TransientError):
            facebook_publisher.publish("facebook", {"text": "x", "media_paths": [str(path)]}, CREDENTIALS)

    @responses.activate
    def test_expired_token_during_upload_is_not_retried_as_plain_transient(self, tmp_path):
        # A code=190 (OAuthException) error must surface as TokenExpiredError
        # straight away, NOT get caught by the resumable-retry loop and
        # relabeled as a generic TransientError — see the docstring on
        # app/publishers/facebook.py::_upload_video_binary.
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"0123456789ABCDEF")
        session_id = "SESSION_EXPIRED"
        upload_url = _upload_binary_url(session_id)

        responses.add(responses.POST, _UPLOADS_URL, json={"id": f"upload:{session_id}"}, status=200)
        responses.add(
            responses.POST,
            upload_url,
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )

        with pytest.raises(TokenExpiredError):
            facebook_publisher.publish("facebook", {"text": "x", "media_paths": [str(path)]}, CREDENTIALS)

        upload_posts = [c for c in responses.calls if c.request.method == "POST" and c.request.url == upload_url]
        assert len(upload_posts) == 1  # not retried

    def test_missing_app_id_is_permanent(self, monkeypatch, tmp_path):
        monkeypatch.delenv("META_APP_ID", raising=False)
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"0123456789ABCDEF")
        with pytest.raises(PermanentError, match="META_APP_ID"):
            facebook_publisher.publish("facebook", {"text": "x", "media_paths": [str(path)]}, CREDENTIALS)


class TestGraphErrorClassification:
    @responses.activate
    def test_http_500_is_transient(self):
        responses.add(responses.POST, _FEED_URL, json={"error": {"message": "oops", "code": 2}}, status=500)
        with pytest.raises(TransientError):
            facebook_publisher.publish("facebook", {"text": "hi"}, CREDENTIALS)

    @responses.activate
    def test_http_429_is_transient(self):
        responses.add(responses.POST, _FEED_URL, json={"error": {"message": "rate limited", "code": 4}}, status=429)
        with pytest.raises(TransientError):
            facebook_publisher.publish("facebook", {"text": "hi"}, CREDENTIALS)

    @responses.activate
    def test_invalid_token_is_token_expired_not_permanent(self):
        responses.add(
            responses.POST,
            _FEED_URL,
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )
        with pytest.raises(TokenExpiredError):
            facebook_publisher.publish("facebook", {"text": "hi"}, CREDENTIALS)

    @responses.activate
    def test_other_4xx_is_permanent(self):
        responses.add(responses.POST, _FEED_URL, json={"error": {"message": "Bad request", "code": 100}}, status=400)
        with pytest.raises(PermanentError):
            facebook_publisher.publish("facebook", {"text": "hi"}, CREDENTIALS)
