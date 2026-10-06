"""
Tests for app/publishers/pinterest.py (Phase 29e): credential resolution,
payload validation, image Pin creation, the 3-step video upload flow
(register -> upload binary to S3 -> poll processing -> create Pin), board
listing, OAuth (build_authorization_url/exchange_code_for_credentials, no
PKCE) and token refresh (rotating, like Twitter's). All HTTP is mocked with
`responses` — nothing here talks to the real Pinterest API.
"""

import pytest
import requests
import responses

from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.publishers import pinterest as pinterest_publisher

CREDENTIALS = {"access_token": "access-tok-1"}

_PINS_URL = f"{pinterest_publisher._API_BASE}/pins"
_MEDIA_REGISTER_URL = f"{pinterest_publisher._API_BASE}/media"
_BOARDS_URL = f"{pinterest_publisher._API_BASE}/boards"
_TOKEN_URL = pinterest_publisher._TOKEN_URL

BASE_PAYLOAD = {"board_id": "board-1", "title": "A nice Pin"}


def _media_status_url(media_id: str) -> str:
    return f"{pinterest_publisher._API_BASE}/media/{media_id}"


@pytest.fixture(autouse=True)
def app_credentials(monkeypatch):
    monkeypatch.setenv("PINTEREST_APP_ID", "app-id-1")
    monkeypatch.setenv("PINTEREST_APP_SECRET", "app-secret-1")


class TestCredentialResolution:
    def test_no_account_credentials_is_permanent(self):
        with pytest.raises(PermanentError, match="requires an Account"):
            pinterest_publisher.publish("pinterest", {**BASE_PAYLOAD, "media_public_url": "https://x/a.png"}, None)

    def test_missing_access_token_is_permanent(self):
        with pytest.raises(PermanentError, match="access_token"):
            pinterest_publisher.publish("pinterest", {**BASE_PAYLOAD, "media_public_url": "https://x/a.png"}, {})


class TestValidatePayload:
    @responses.activate
    def test_missing_board_id_is_permanent_no_http(self):
        with pytest.raises(PermanentError, match="board_id"):
            pinterest_publisher.publish("pinterest", {"title": "t", "media_public_url": "https://x/a.png"}, CREDENTIALS)

    @responses.activate
    def test_missing_title_is_permanent_no_http(self):
        with pytest.raises(PermanentError, match="title"):
            pinterest_publisher.publish("pinterest", {"board_id": "b", "media_public_url": "https://x/a.png"}, CREDENTIALS)

    @responses.activate
    def test_no_media_is_permanent_no_http(self):
        with pytest.raises(PermanentError, match="media"):
            pinterest_publisher.publish("pinterest", BASE_PAYLOAD, CREDENTIALS)

    @responses.activate
    def test_both_media_sources_is_permanent_no_http(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"fake video")
        payload = {**BASE_PAYLOAD, "media_public_url": "https://x/a.png", "media_paths": [str(path)]}
        with pytest.raises(PermanentError, match="cannot include both"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    @responses.activate
    def test_video_via_public_url_is_permanent_no_http(self):
        payload = {**BASE_PAYLOAD, "media_public_url": "https://x/clip.mp4"}
        with pytest.raises(PermanentError, match="local-file upload flow"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    @responses.activate
    def test_image_via_media_paths_is_permanent_no_http(self, tmp_path):
        path = tmp_path / "photo.png"
        path.write_bytes(b"\x89PNG fake")
        payload = {**BASE_PAYLOAD, "media_paths": [str(path)]}
        with pytest.raises(PermanentError, match="only used for Pinterest video pins"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    def test_missing_file_is_permanent(self):
        payload = {**BASE_PAYLOAD, "media_paths": ["/nope/missing.mp4"]}
        with pytest.raises(PermanentError, match="not found"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    def test_more_than_one_media_path_is_permanent(self, tmp_path):
        p1 = tmp_path / "a.mp4"
        p2 = tmp_path / "b.mp4"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")
        payload = {**BASE_PAYLOAD, "media_paths": [str(p1), str(p2)]}
        with pytest.raises(PermanentError, match="exactly one"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)


class TestImagePin:
    @responses.activate
    def test_happy_path(self):
        responses.add(responses.POST, _PINS_URL, json={"id": "pin-1"}, status=201)

        payload = {**BASE_PAYLOAD, "media_public_url": "https://x/a.png", "description": "a desc", "link": "https://x/page"}
        result = pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

        assert result == {"platform": "pinterest", "external_id": "pin-1", "board_id": "board-1"}
        sent = responses.calls[0].request
        assert sent.headers["Authorization"] == "Bearer access-tok-1"
        import json

        body = json.loads(sent.body)
        assert body["board_id"] == "board-1"
        assert body["title"] == "A nice Pin"
        assert body["description"] == "a desc"
        assert body["link"] == "https://x/page"
        assert body["media_source"] == {"source_type": "image_url", "url": "https://x/a.png"}

    @responses.activate
    def test_description_falls_back_to_text(self):
        responses.add(responses.POST, _PINS_URL, json={"id": "pin-1"}, status=201)
        payload = {**BASE_PAYLOAD, "media_public_url": "https://x/a.png", "text": "caption as description"}
        pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

        import json

        body = json.loads(responses.calls[0].request.body)
        assert body["description"] == "caption as description"

    @responses.activate
    def test_missing_response_id_is_permanent(self):
        responses.add(responses.POST, _PINS_URL, json={}, status=201)
        payload = {**BASE_PAYLOAD, "media_public_url": "https://x/a.png"}
        with pytest.raises(PermanentError, match="missing id"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)


class TestVideoPin:
    @responses.activate
    def test_happy_path_default_cover_frame(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"0123456789ABCDEF")
        upload_url = "https://s3.example.com/upload-session-abc"

        responses.add(
            responses.POST,
            _MEDIA_REGISTER_URL,
            json={"media_id": "media-1", "upload_url": upload_url, "upload_parameters": {"key": "value1"}},
            status=201,
        )
        responses.add(responses.POST, upload_url, status=204)
        responses.add(responses.GET, _media_status_url("media-1"), json={"status": "succeeded"}, status=200)
        responses.add(responses.POST, _PINS_URL, json={"id": "pin-video-1"}, status=201)

        payload = {**BASE_PAYLOAD, "media_paths": [str(path)]}
        result = pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

        assert result == {"platform": "pinterest", "external_id": "pin-video-1", "board_id": "board-1"}

        import json

        pin_body = json.loads(responses.calls[-1].request.body)
        assert pin_body["media_source"] == {
            "source_type": "video_id",
            "media_id": "media-1",
            "cover_image_key_frame_time": 0,
        }

    @responses.activate
    def test_happy_path_explicit_cover_image_url(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"0123456789ABCDEF")
        upload_url = "https://s3.example.com/upload-session-xyz"

        responses.add(
            responses.POST,
            _MEDIA_REGISTER_URL,
            json={"media_id": "media-2", "upload_url": upload_url, "upload_parameters": {}},
            status=201,
        )
        responses.add(responses.POST, upload_url, status=204)
        responses.add(responses.GET, _media_status_url("media-2"), json={"status": "succeeded"}, status=200)
        responses.add(responses.POST, _PINS_URL, json={"id": "pin-video-2"}, status=201)

        payload = {**BASE_PAYLOAD, "media_paths": [str(path)], "cover_image_url": "https://x/cover.png"}
        pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

        import json

        pin_body = json.loads(responses.calls[-1].request.body)
        assert pin_body["media_source"]["cover_image_url"] == "https://x/cover.png"
        assert "cover_image_key_frame_time" not in pin_body["media_source"]

    @responses.activate
    def test_processing_failed_is_permanent(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"x")
        upload_url = "https://s3.example.com/upload-session-fail"

        responses.add(
            responses.POST,
            _MEDIA_REGISTER_URL,
            json={"media_id": "media-3", "upload_url": upload_url, "upload_parameters": {}},
            status=201,
        )
        responses.add(responses.POST, upload_url, status=204)
        responses.add(responses.GET, _media_status_url("media-3"), json={"status": "failed"}, status=200)

        payload = {**BASE_PAYLOAD, "media_paths": [str(path)]}
        with pytest.raises(PermanentError, match="failed to process"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    @responses.activate
    def test_processing_timeout_is_transient(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pinterest_publisher, "_MEDIA_POLL_TIMEOUT_SECONDS", 0)
        monkeypatch.setattr(pinterest_publisher, "_MEDIA_POLL_INTERVAL_SECONDS", 0)

        path = tmp_path / "clip.mp4"
        path.write_bytes(b"x")
        upload_url = "https://s3.example.com/upload-session-timeout"

        responses.add(
            responses.POST,
            _MEDIA_REGISTER_URL,
            json={"media_id": "media-4", "upload_url": upload_url, "upload_parameters": {}},
            status=201,
        )
        responses.add(responses.POST, upload_url, status=204)
        responses.add(responses.GET, _media_status_url("media-4"), json={"status": "in_progress"}, status=200)

        payload = {**BASE_PAYLOAD, "media_paths": [str(path)]}
        with pytest.raises(TransientError, match="Timed out"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    @responses.activate
    def test_s3_upload_failure_is_transient(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"x")
        upload_url = "https://s3.example.com/upload-session-bad"

        responses.add(
            responses.POST,
            _MEDIA_REGISTER_URL,
            json={"media_id": "media-5", "upload_url": upload_url, "upload_parameters": {}},
            status=201,
        )
        responses.add(responses.POST, upload_url, status=500, body="internal error")

        payload = {**BASE_PAYLOAD, "media_paths": [str(path)]}
        with pytest.raises(TransientError, match="media storage"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    @responses.activate
    def test_register_response_missing_fields_is_permanent(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"x")
        responses.add(responses.POST, _MEDIA_REGISTER_URL, json={"media_id": "media-6"}, status=201)

        payload = {**BASE_PAYLOAD, "media_paths": [str(path)]}
        with pytest.raises(PermanentError, match="missing required fields"):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)


class TestErrorClassification:
    @responses.activate
    def test_http_500_is_transient(self):
        responses.add(responses.POST, _PINS_URL, json={"message": "oops"}, status=500)
        payload = {**BASE_PAYLOAD, "media_public_url": "https://x/a.png"}
        with pytest.raises(TransientError):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    @responses.activate
    def test_http_429_is_transient(self):
        responses.add(responses.POST, _PINS_URL, json={"message": "rate limited"}, status=429)
        payload = {**BASE_PAYLOAD, "media_public_url": "https://x/a.png"}
        with pytest.raises(TransientError):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    @responses.activate
    def test_http_401_is_token_expired_not_permanent(self):
        responses.add(responses.POST, _PINS_URL, json={"message": "invalid token"}, status=401)
        payload = {**BASE_PAYLOAD, "media_public_url": "https://x/a.png"}
        with pytest.raises(TokenExpiredError):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)

    @responses.activate
    def test_other_4xx_is_permanent(self):
        responses.add(responses.POST, _PINS_URL, json={"message": "bad request"}, status=400)
        payload = {**BASE_PAYLOAD, "media_public_url": "https://x/a.png"}
        with pytest.raises(PermanentError):
            pinterest_publisher.publish("pinterest", payload, CREDENTIALS)


class TestListBoards:
    @responses.activate
    def test_happy_path_single_page(self):
        responses.add(
            responses.GET,
            _BOARDS_URL,
            json={"items": [{"id": "b1", "name": "Board One"}, {"id": "b2", "name": "Board Two"}]},
            status=200,
        )

        boards = pinterest_publisher.list_boards(CREDENTIALS)

        assert boards == [{"id": "b1", "name": "Board One"}, {"id": "b2", "name": "Board Two"}]

    @responses.activate
    def test_paginates_via_bookmark(self):
        responses.add(
            responses.GET,
            _BOARDS_URL,
            json={"items": [{"id": "b1", "name": "Board One"}], "bookmark": "cursor-1"},
            status=200,
        )
        responses.add(
            responses.GET,
            _BOARDS_URL,
            json={"items": [{"id": "b2", "name": "Board Two"}]},
            status=200,
        )

        boards = pinterest_publisher.list_boards(CREDENTIALS)

        assert boards == [{"id": "b1", "name": "Board One"}, {"id": "b2", "name": "Board Two"}]
        assert len(responses.calls) == 2
        assert "bookmark=cursor-1" in responses.calls[1].request.url

    @responses.activate
    def test_401_is_permanent_not_token_expired(self):
        responses.add(responses.GET, _BOARDS_URL, json={"message": "invalid token"}, status=401)
        with pytest.raises(PermanentError):
            pinterest_publisher.list_boards(CREDENTIALS)

    def test_missing_access_token_is_permanent(self):
        with pytest.raises(PermanentError, match="access_token"):
            pinterest_publisher.list_boards({})


class TestTokenRefresh:
    def test_missing_refresh_token_is_permanent(self):
        with pytest.raises(PermanentError, match="refresh_token"):
            pinterest_publisher.refresh_stored_credentials({})

    @responses.activate
    def test_happy_path_rotation(self):
        responses.add(
            responses.POST,
            _TOKEN_URL,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 2592000},
            status=200,
        )

        new_creds = pinterest_publisher.refresh_stored_credentials({"refresh_token": "old-refresh"})

        assert new_creds["access_token"] == "new-access"
        assert new_creds["refresh_token"] == "new-refresh"
        assert new_creds["expires_at"] is not None

        request = responses.calls[0].request
        assert request.headers["Authorization"].startswith("Basic ")
        assert "grant_type=refresh_token" in request.body
        assert "refresh_token=old-refresh" in request.body

    @responses.activate
    def test_missing_rotated_refresh_token_is_transient(self):
        responses.add(responses.POST, _TOKEN_URL, json={"access_token": "new-access", "expires_in": 2592000}, status=200)
        with pytest.raises(TransientError, match="rotated refresh_token"):
            pinterest_publisher.refresh_stored_credentials({"refresh_token": "old-refresh"})

    @responses.activate
    def test_invalid_grant_is_permanent(self):
        responses.add(
            responses.POST,
            _TOKEN_URL,
            json={"error": "invalid_grant", "error_description": "refresh token revoked"},
            status=400,
        )
        with pytest.raises(PermanentError, match="invalid_grant"):
            pinterest_publisher.refresh_stored_credentials({"refresh_token": "old-refresh"})

    @responses.activate
    def test_server_error_is_transient(self):
        responses.add(responses.POST, _TOKEN_URL, json={"error": "server_error"}, status=500)
        with pytest.raises(TransientError):
            pinterest_publisher.refresh_stored_credentials({"refresh_token": "old-refresh"})

    def test_missing_app_credentials_is_permanent(self, monkeypatch):
        monkeypatch.delenv("PINTEREST_APP_ID", raising=False)
        with pytest.raises(PermanentError, match="PINTEREST_APP_ID"):
            pinterest_publisher.refresh_stored_credentials({"refresh_token": "old-refresh"})


class TestTokenExpiresWithin:
    def test_missing_expires_at_needs_refresh(self):
        assert pinterest_publisher.token_expires_within({}, 60) is True

    def test_unparseable_expires_at_needs_refresh(self):
        assert pinterest_publisher.token_expires_within({"expires_at": "not-a-date"}, 60) is True

    def test_far_future_expiry_does_not_need_refresh(self):
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        assert pinterest_publisher.token_expires_within({"expires_at": future}, 60) is False

    def test_near_expiry_needs_refresh(self):
        from datetime import datetime, timedelta, timezone

        soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        assert pinterest_publisher.token_expires_within({"expires_at": soon}, 60) is True


class TestWebOAuthFlow:
    """Phase 29e — build_authorization_url/exchange_code_for_credentials, the
    in-browser counterpart to a CLI authorize script (Pinterest has none —
    this is its only authorization flow). Modeled on
    tests/test_publisher_twitter.py::TestWebOAuthFlow, minus every PKCE-
    specific assertion (Pinterest's OAuth dialog needs none)."""

    def test_build_authorization_url_missing_app_id_raises(self, monkeypatch):
        monkeypatch.delenv("PINTEREST_APP_ID", raising=False)
        with pytest.raises(PermanentError):
            pinterest_publisher.build_authorization_url("http://localhost/callback", "state123")

    def test_build_authorization_url_happy_path(self):
        url = pinterest_publisher.build_authorization_url("http://localhost/callback", "state123")

        assert url.startswith(pinterest_publisher.AUTHORIZE_URL + "?")
        assert "client_id=app-id-1" in url
        assert "state=state123" in url
        assert "response_type=code" in url
        assert "code_challenge" not in url

    def test_exchange_code_for_credentials_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("PINTEREST_APP_ID", raising=False)
        monkeypatch.delenv("PINTEREST_APP_SECRET", raising=False)
        with pytest.raises(PermanentError):
            pinterest_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback")

    @responses.activate
    def test_exchange_code_for_credentials_happy_path(self):
        responses.add(
            responses.POST,
            _TOKEN_URL,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 2592000},
            status=200,
        )

        creds = pinterest_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback")

        assert creds == {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": creds["expires_at"],
        }
        assert creds["expires_at"] is not None

        request = responses.calls[0].request
        assert request.headers["Authorization"].startswith("Basic ")
        assert "grant_type=authorization_code" in request.body
        assert "code=auth-code" in request.body

    @responses.activate
    def test_exchange_code_for_credentials_missing_refresh_token_is_permanent(self):
        responses.add(responses.POST, _TOKEN_URL, json={"access_token": "new-access"}, status=200)
        with pytest.raises(PermanentError):
            pinterest_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback")

    @responses.activate
    def test_exchange_code_for_credentials_wraps_token_endpoint_failure_as_permanent(self):
        responses.add(
            responses.POST,
            _TOKEN_URL,
            json={"error": "invalid_grant", "error_description": "code already used"},
            status=400,
        )
        with pytest.raises(PermanentError, match="invalid_grant"):
            pinterest_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback")
