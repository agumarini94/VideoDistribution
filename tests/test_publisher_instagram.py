"""
Tests for app/publishers/instagram.py (Phase 25): credential resolution,
missing/required media_public_url, image vs. Reels-video container creation,
the container-status poll (IN_PROGRESS -> FINISHED, ERROR, EXPIRED, timeout),
publishing the container, and Graph error classification (including
code=190 -> TokenExpiredError). All HTTP is mocked with `responses` — nothing
here talks to the real Graph API, and time.sleep is monkeypatched so poll
tests don't actually wait.
"""

import pytest
import responses

from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.publishers import instagram as instagram_publisher
from app.publishers import meta as meta_publisher

IG_USER_ID = "ig-1"

CREDENTIALS = {"ig_user_id": IG_USER_ID, "page_id": "page-1", "page_token": "page-tok"}

_MEDIA_URL = f"{meta_publisher.GRAPH_API_BASE}/{IG_USER_ID}/media"
_MEDIA_PUBLISH_URL = f"{meta_publisher.GRAPH_API_BASE}/{IG_USER_ID}/media_publish"

IMAGE_URL = "https://pub-example.r2.dev/2026/09/02/abc123.jpg"
VIDEO_URL = "https://pub-example.r2.dev/2026/09/02/abc123.mp4"


def _container_status_url(container_id: str) -> str:
    return f"{meta_publisher.GRAPH_API_BASE}/{container_id}"


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(instagram_publisher.time, "sleep", lambda seconds: None)


class TestCredentialResolution:
    def test_no_account_credentials_is_permanent(self):
        with pytest.raises(PermanentError, match="requires an Account"):
            instagram_publisher.publish("instagram", {"media_public_url": IMAGE_URL}, None)

    def test_missing_fields_are_permanent(self):
        with pytest.raises(PermanentError, match="page_token"):
            instagram_publisher.publish("instagram", {"media_public_url": IMAGE_URL}, {"ig_user_id": IG_USER_ID})

    def test_missing_media_url_is_permanent(self):
        with pytest.raises(PermanentError, match="media_public_url"):
            instagram_publisher.publish("instagram", {"text": "hi"}, CREDENTIALS)

    def test_undetectable_media_type_is_permanent(self):
        with pytest.raises(PermanentError, match="Unsupported or undetectable"):
            instagram_publisher.publish(
                "instagram", {"media_public_url": "https://pub-example.r2.dev/no-extension"}, CREDENTIALS
            )


class TestImagePost:
    @responses.activate
    def test_happy_path_with_caption(self):
        responses.add(responses.POST, _MEDIA_URL, json={"id": "container-1"}, status=200)
        responses.add(
            responses.GET, _container_status_url("container-1"), json={"status_code": "FINISHED"}, status=200
        )
        responses.add(responses.POST, _MEDIA_PUBLISH_URL, json={"id": "ig-media-1"}, status=200)

        result = instagram_publisher.publish(
            "instagram", {"text": "look at this", "media_public_url": IMAGE_URL}, CREDENTIALS
        )

        assert result == {"platform": "instagram", "external_id": "ig-media-1"}

        create_call = responses.calls[0]
        assert f"image_url={IMAGE_URL}" in create_call.request.body or "image_url" in create_call.request.body
        assert "media_type" not in create_call.request.body

        publish_call = responses.calls[-1]
        assert "creation_id=container-1" in publish_call.request.body

    @responses.activate
    def test_happy_path_without_caption(self):
        responses.add(responses.POST, _MEDIA_URL, json={"id": "container-2"}, status=200)
        responses.add(
            responses.GET, _container_status_url("container-2"), json={"status_code": "FINISHED"}, status=200
        )
        responses.add(responses.POST, _MEDIA_PUBLISH_URL, json={"id": "ig-media-2"}, status=200)

        result = instagram_publisher.publish("instagram", {"media_public_url": IMAGE_URL}, CREDENTIALS)

        assert result == {"platform": "instagram", "external_id": "ig-media-2"}
        create_call = responses.calls[0]
        assert "caption" not in create_call.request.body


class TestVideoPost:
    @responses.activate
    def test_reels_flow_with_in_progress_then_finished(self):
        responses.add(responses.POST, _MEDIA_URL, json={"id": "container-3"}, status=200)
        responses.add(
            responses.GET, _container_status_url("container-3"), json={"status_code": "IN_PROGRESS"}, status=200
        )
        responses.add(
            responses.GET, _container_status_url("container-3"), json={"status_code": "IN_PROGRESS"}, status=200
        )
        responses.add(
            responses.GET, _container_status_url("container-3"), json={"status_code": "FINISHED"}, status=200
        )
        responses.add(responses.POST, _MEDIA_PUBLISH_URL, json={"id": "ig-media-3"}, status=200)

        result = instagram_publisher.publish(
            "instagram", {"text": "a reel", "media_public_url": VIDEO_URL}, CREDENTIALS
        )

        assert result == {"platform": "instagram", "external_id": "ig-media-3"}

        create_call = responses.calls[0]
        assert "media_type=REELS" in create_call.request.body
        assert "video_url" in create_call.request.body

        status_calls = [c for c in responses.calls if c.request.url.startswith(_container_status_url("container-3"))]
        assert len(status_calls) == 3

    @responses.activate
    def test_container_error_status_is_permanent(self):
        responses.add(responses.POST, _MEDIA_URL, json={"id": "container-4"}, status=200)
        responses.add(responses.GET, _container_status_url("container-4"), json={"status_code": "ERROR"}, status=200)

        with pytest.raises(PermanentError, match="ERROR"):
            instagram_publisher.publish("instagram", {"media_public_url": VIDEO_URL}, CREDENTIALS)

    @responses.activate
    def test_container_expired_status_is_permanent(self):
        responses.add(responses.POST, _MEDIA_URL, json={"id": "container-5"}, status=200)
        responses.add(
            responses.GET, _container_status_url("container-5"), json={"status_code": "EXPIRED"}, status=200
        )

        with pytest.raises(PermanentError, match="EXPIRED"):
            instagram_publisher.publish("instagram", {"media_public_url": VIDEO_URL}, CREDENTIALS)

    @responses.activate
    def test_poll_timeout_is_transient(self, monkeypatch):
        monkeypatch.setattr(instagram_publisher, "_POLL_INTERVAL_SECONDS", 5)
        monkeypatch.setattr(instagram_publisher, "_POLL_TIMEOUT_SECONDS", 10)

        responses.add(responses.POST, _MEDIA_URL, json={"id": "container-6"}, status=200)
        responses.add(
            responses.GET, _container_status_url("container-6"), json={"status_code": "IN_PROGRESS"}, status=200
        )

        with pytest.raises(TransientError, match="Timed out"):
            instagram_publisher.publish("instagram", {"media_public_url": VIDEO_URL}, CREDENTIALS)

    @responses.activate
    def test_missing_container_id_is_permanent(self):
        responses.add(responses.POST, _MEDIA_URL, json={}, status=200)
        with pytest.raises(PermanentError, match="missing id"):
            instagram_publisher.publish("instagram", {"media_public_url": VIDEO_URL}, CREDENTIALS)


class TestGraphErrorClassification:
    @responses.activate
    def test_http_500_on_container_create_is_transient(self):
        responses.add(responses.POST, _MEDIA_URL, json={"error": {"message": "oops", "code": 2}}, status=500)
        with pytest.raises(TransientError):
            instagram_publisher.publish("instagram", {"media_public_url": IMAGE_URL}, CREDENTIALS)

    @responses.activate
    def test_http_429_on_container_create_is_transient(self):
        responses.add(
            responses.POST, _MEDIA_URL, json={"error": {"message": "rate limited", "code": 4}}, status=429
        )
        with pytest.raises(TransientError):
            instagram_publisher.publish("instagram", {"media_public_url": IMAGE_URL}, CREDENTIALS)

    @responses.activate
    def test_invalid_token_on_container_create_is_token_expired(self):
        responses.add(
            responses.POST,
            _MEDIA_URL,
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )
        with pytest.raises(TokenExpiredError):
            instagram_publisher.publish("instagram", {"media_public_url": IMAGE_URL}, CREDENTIALS)

    @responses.activate
    def test_invalid_token_during_status_poll_is_token_expired(self):
        responses.add(responses.POST, _MEDIA_URL, json={"id": "container-7"}, status=200)
        responses.add(
            responses.GET,
            _container_status_url("container-7"),
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )
        with pytest.raises(TokenExpiredError):
            instagram_publisher.publish("instagram", {"media_public_url": IMAGE_URL}, CREDENTIALS)

    @responses.activate
    def test_invalid_token_during_publish_is_token_expired(self):
        responses.add(responses.POST, _MEDIA_URL, json={"id": "container-8"}, status=200)
        responses.add(
            responses.GET, _container_status_url("container-8"), json={"status_code": "FINISHED"}, status=200
        )
        responses.add(
            responses.POST,
            _MEDIA_PUBLISH_URL,
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )
        with pytest.raises(TokenExpiredError):
            instagram_publisher.publish("instagram", {"media_public_url": IMAGE_URL}, CREDENTIALS)

    @responses.activate
    def test_other_4xx_is_permanent(self):
        responses.add(
            responses.POST, _MEDIA_URL, json={"error": {"message": "Bad request", "code": 100}}, status=400
        )
        with pytest.raises(PermanentError):
            instagram_publisher.publish("instagram", {"media_public_url": IMAGE_URL}, CREDENTIALS)
