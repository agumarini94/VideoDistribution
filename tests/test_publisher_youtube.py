"""
Tests for app/publishers/youtube.py Phase 15: Shorts validation (duration +
aspect ratio, via app/media_probe.py) and playlist assignment.

No real HTTP and no real ffprobe: app.media_probe.probe is monkeypatched
directly (see tests/test_media_probe.py for probe()'s own subprocess
mocking), credential loading is monkeypatched to skip real OAuth, and the
YouTube API itself is replaced with an in-memory fake service object
(googleapiclient talks httplib2, not `requests`, so `responses` doesn't
apply here the way it does for the tiktok/twitter publishers).
"""

import pytest

from app import media_probe
from app.exceptions import PermanentError
from app.publishers import youtube as youtube_publisher

CREDENTIALS = {"access_token": "unused-fake-creds"}


class _FakeCredentials:
    expired = False
    valid = True
    refresh_token = "refresh"

    def to_json(self):
        return "{}"


class _Execute:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeVideosResource:
    def __init__(self, video_id):
        self.video_id = video_id
        self.insert_calls = []

    def insert(self, part, body, media_body):
        self.insert_calls.append({"part": part, "body": body})
        return _Execute(result={"id": self.video_id})


class _FakePlaylistItemsResource:
    def __init__(self, error=None):
        self.error = error
        self.insert_calls = []

    def insert(self, part, body):
        self.insert_calls.append({"part": part, "body": body})
        return _Execute(result={"id": "playlist-item-1"}, error=self.error)


class _FakeYouTubeService:
    def __init__(self, video_id="vid-123", playlist_error=None):
        self._videos = _FakeVideosResource(video_id)
        self._playlist_items = _FakePlaylistItemsResource(error=playlist_error)

    def videos(self):
        return self._videos

    def playlistItems(self):
        return self._playlist_items


@pytest.fixture(autouse=True)
def _stub_credentials(monkeypatch):
    monkeypatch.setattr(youtube_publisher, "_load_credentials", lambda account_credentials: (_FakeCredentials(), False))


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake video bytes")
    return path


def _payload(video_path, **extra):
    return {"video_path": str(video_path), "title": "Test video", **extra}


class TestShortsValidation:
    def test_over_60s_raises_permanent_error_without_uploading(self, monkeypatch, video_file):
        monkeypatch.setattr(media_probe, "probe", lambda path: {"duration_seconds": 61.0, "width": 1080, "height": 1920})
        fake_service = _FakeYouTubeService()
        monkeypatch.setattr(youtube_publisher, "build", lambda *a, **k: fake_service)

        with pytest.raises(PermanentError, match="61"):
            youtube_publisher.publish("youtube", _payload(video_file, shorts=True), CREDENTIALS)

        assert fake_service._videos.insert_calls == []

    def test_horizontal_video_raises_permanent_error_without_uploading(self, monkeypatch, video_file):
        monkeypatch.setattr(media_probe, "probe", lambda path: {"duration_seconds": 30.0, "width": 1920, "height": 1080})
        fake_service = _FakeYouTubeService()
        monkeypatch.setattr(youtube_publisher, "build", lambda *a, **k: fake_service)

        with pytest.raises(PermanentError, match="1920x1080"):
            youtube_publisher.publish("youtube", _payload(video_file, shorts=True), CREDENTIALS)

        assert fake_service._videos.insert_calls == []

    def test_valid_vertical_short_uploads(self, monkeypatch, video_file):
        monkeypatch.setattr(media_probe, "probe", lambda path: {"duration_seconds": 30.0, "width": 1080, "height": 1920})
        fake_service = _FakeYouTubeService(video_id="short-1")
        monkeypatch.setattr(youtube_publisher, "build", lambda *a, **k: fake_service)

        result = youtube_publisher.publish("youtube", _payload(video_file, shorts=True), CREDENTIALS)

        assert result["external_id"] == "short-1"
        assert len(fake_service._videos.insert_calls) == 1

    def test_shorts_absent_skips_probing(self, monkeypatch, video_file):
        def _fail_probe(path):
            raise AssertionError("probe() should not be called when 'shorts' is absent")

        monkeypatch.setattr(media_probe, "probe", _fail_probe)
        fake_service = _FakeYouTubeService(video_id="regular-1")
        monkeypatch.setattr(youtube_publisher, "build", lambda *a, **k: fake_service)

        result = youtube_publisher.publish("youtube", _payload(video_file), CREDENTIALS)

        assert result["external_id"] == "regular-1"


class TestPlaylistAssignment:
    def test_playlist_success_adds_item(self, monkeypatch, video_file):
        fake_service = _FakeYouTubeService(video_id="vid-1")
        monkeypatch.setattr(youtube_publisher, "build", lambda *a, **k: fake_service)

        result = youtube_publisher.publish("youtube", _payload(video_file, playlist_id="PL123"), CREDENTIALS)

        assert result == {"platform": "youtube", "external_id": "vid-1"}
        assert len(fake_service._playlist_items.insert_calls) == 1
        body = fake_service._playlist_items.insert_calls[0]["body"]
        assert body["snippet"]["playlistId"] == "PL123"
        assert body["snippet"]["resourceId"]["videoId"] == "vid-1"

    def test_playlist_failure_still_returns_external_id_with_playlist_error(self, monkeypatch, video_file):
        fake_service = _FakeYouTubeService(video_id="vid-2", playlist_error=RuntimeError("insufficient scope"))
        monkeypatch.setattr(youtube_publisher, "build", lambda *a, **k: fake_service)

        result = youtube_publisher.publish("youtube", _payload(video_file, playlist_id="PL123"), CREDENTIALS)

        assert result["external_id"] == "vid-2"
        assert "insufficient scope" in result["playlist_error"]

    def test_no_playlist_id_skips_playlist_call(self, monkeypatch, video_file):
        fake_service = _FakeYouTubeService(video_id="vid-3")
        monkeypatch.setattr(youtube_publisher, "build", lambda *a, **k: fake_service)

        result = youtube_publisher.publish("youtube", _payload(video_file), CREDENTIALS)

        assert "playlist_error" not in result
        assert fake_service._playlist_items.insert_calls == []
