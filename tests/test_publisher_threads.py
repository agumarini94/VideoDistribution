"""
Tests for app/publishers/threads.py (Phase 29f): credential resolution,
payload validation (text/media, 500-char limit, no carousel), the
container-create -> poll -> publish flow for text/image/video, Graph-style
error classification (including code=190 -> TokenExpiredError), and OAuth
(build_authorization_url / exchange_code_for_credentials, no PKCE) plus
token refresh (non-rotating, like Meta's). All HTTP is mocked with
`responses` — nothing here talks to the real Threads API, and time.sleep is
monkeypatched so poll tests don't actually wait.
"""

import pytest
import responses

from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.publishers import threads as threads_publisher

THREADS_USER_ID = "threads-user-1"

CREDENTIALS = {"threads_user_id": THREADS_USER_ID, "access_token": "access-tok-1"}

_CONTAINER_CREATE_URL = f"{threads_publisher._API_BASE}/{THREADS_USER_ID}/threads"
_PUBLISH_URL = f"{threads_publisher._API_BASE}/{THREADS_USER_ID}/threads_publish"
_ME_URL = f"{threads_publisher._API_BASE}/me"
_TOKEN_URL = threads_publisher._TOKEN_URL
_LONG_LIVED_EXCHANGE_URL = threads_publisher._LONG_LIVED_EXCHANGE_URL
_REFRESH_URL = threads_publisher._REFRESH_URL

IMAGE_URL = "https://pub-example.r2.dev/2026/09/02/abc123.jpg"
VIDEO_URL = "https://pub-example.r2.dev/2026/09/02/abc123.mp4"


def _container_status_url(creation_id: str) -> str:
    return f"{threads_publisher._API_BASE}/{creation_id}"


@pytest.fixture(autouse=True)
def app_credentials(monkeypatch):
    monkeypatch.setenv("THREADS_APP_ID", "app-id-1")
    monkeypatch.setenv("THREADS_APP_SECRET", "app-secret-1")


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(threads_publisher.time, "sleep", lambda seconds: None)


class TestCredentialResolution:
    def test_no_account_credentials_is_permanent(self):
        with pytest.raises(PermanentError, match="requires an Account"):
            threads_publisher.publish("threads", {"text": "hi"}, None)

    def test_missing_access_token_is_permanent(self):
        with pytest.raises(PermanentError, match="access_token"):
            threads_publisher.publish("threads", {"text": "hi"}, {"threads_user_id": THREADS_USER_ID})

    def test_missing_threads_user_id_is_permanent(self):
        with pytest.raises(PermanentError, match="threads_user_id"):
            threads_publisher.publish("threads", {"text": "hi"}, {"access_token": "tok"})


class TestValidatePayload:
    @responses.activate
    def test_no_text_and_no_media_is_permanent_no_http(self):
        with pytest.raises(PermanentError, match="text.*media_public_url"):
            threads_publisher.publish("threads", {}, CREDENTIALS)

    @responses.activate
    def test_text_over_limit_is_permanent_no_http(self):
        payload = {"text": "x" * 501}
        with pytest.raises(PermanentError, match="500 characters"):
            threads_publisher.publish("threads", payload, CREDENTIALS)

    @responses.activate
    def test_undetectable_media_type_is_permanent_no_http(self):
        payload = {"media_public_url": "https://pub-example.r2.dev/no-extension"}
        with pytest.raises(PermanentError, match="Unsupported or undetectable"):
            threads_publisher.publish("threads", payload, CREDENTIALS)


class TestTextPost:
    @responses.activate
    def test_happy_path_exactly_500_chars(self):
        responses.add(responses.POST, _CONTAINER_CREATE_URL, json={"id": "creation-1"}, status=200)
        responses.add(responses.GET, _container_status_url("creation-1"), json={"status": "FINISHED"}, status=200)
        responses.add(responses.POST, _PUBLISH_URL, json={"id": "thread-1"}, status=200)

        text = "x" * 500
        result = threads_publisher.publish("threads", {"text": text}, CREDENTIALS)

        assert result == {"platform": "threads", "external_id": "thread-1"}
        create_call = responses.calls[0]
        assert "media_type=TEXT" in create_call.request.body
        assert "image_url" not in create_call.request.body
        assert "video_url" not in create_call.request.body

        publish_call = responses.calls[-1]
        assert "creation_id=creation-1" in publish_call.request.body


class TestImagePost:
    @responses.activate
    def test_happy_path_with_text(self):
        responses.add(responses.POST, _CONTAINER_CREATE_URL, json={"id": "creation-2"}, status=200)
        responses.add(responses.GET, _container_status_url("creation-2"), json={"status": "FINISHED"}, status=200)
        responses.add(responses.POST, _PUBLISH_URL, json={"id": "thread-2"}, status=200)

        result = threads_publisher.publish("threads", {"text": "look at this", "media_public_url": IMAGE_URL}, CREDENTIALS)

        assert result == {"platform": "threads", "external_id": "thread-2"}
        create_call = responses.calls[0]
        assert "media_type=IMAGE" in create_call.request.body
        assert "image_url" in create_call.request.body

    @responses.activate
    def test_happy_path_media_only_no_text(self):
        responses.add(responses.POST, _CONTAINER_CREATE_URL, json={"id": "creation-3"}, status=200)
        responses.add(responses.GET, _container_status_url("creation-3"), json={"status": "FINISHED"}, status=200)
        responses.add(responses.POST, _PUBLISH_URL, json={"id": "thread-3"}, status=200)

        result = threads_publisher.publish("threads", {"media_public_url": IMAGE_URL}, CREDENTIALS)

        assert result == {"platform": "threads", "external_id": "thread-3"}
        create_call = responses.calls[0]
        assert "text" not in create_call.request.body


class TestVideoPost:
    @responses.activate
    def test_in_progress_then_finished(self):
        responses.add(responses.POST, _CONTAINER_CREATE_URL, json={"id": "creation-4"}, status=200)
        responses.add(responses.GET, _container_status_url("creation-4"), json={"status": "IN_PROGRESS"}, status=200)
        responses.add(responses.GET, _container_status_url("creation-4"), json={"status": "IN_PROGRESS"}, status=200)
        responses.add(responses.GET, _container_status_url("creation-4"), json={"status": "FINISHED"}, status=200)
        responses.add(responses.POST, _PUBLISH_URL, json={"id": "thread-4"}, status=200)

        result = threads_publisher.publish("threads", {"media_public_url": VIDEO_URL}, CREDENTIALS)

        assert result == {"platform": "threads", "external_id": "thread-4"}
        create_call = responses.calls[0]
        assert "media_type=VIDEO" in create_call.request.body
        assert "video_url" in create_call.request.body

        status_calls = [c for c in responses.calls if c.request.url.startswith(_container_status_url("creation-4"))]
        assert len(status_calls) == 3

    @responses.activate
    def test_container_error_status_is_permanent(self):
        responses.add(responses.POST, _CONTAINER_CREATE_URL, json={"id": "creation-5"}, status=200)
        responses.add(
            responses.GET,
            _container_status_url("creation-5"),
            json={"status": "ERROR", "error_message": "could not process video"},
            status=200,
        )

        with pytest.raises(PermanentError, match="could not process video"):
            threads_publisher.publish("threads", {"media_public_url": VIDEO_URL}, CREDENTIALS)

    @responses.activate
    def test_poll_timeout_is_transient(self, monkeypatch):
        monkeypatch.setattr(threads_publisher, "_POLL_INTERVAL_SECONDS", 5)
        monkeypatch.setattr(threads_publisher, "_POLL_TIMEOUT_SECONDS", 10)

        responses.add(responses.POST, _CONTAINER_CREATE_URL, json={"id": "creation-6"}, status=200)
        responses.add(responses.GET, _container_status_url("creation-6"), json={"status": "IN_PROGRESS"}, status=200)

        with pytest.raises(TransientError, match="Timed out"):
            threads_publisher.publish("threads", {"media_public_url": VIDEO_URL}, CREDENTIALS)

    @responses.activate
    def test_missing_creation_id_is_permanent(self):
        responses.add(responses.POST, _CONTAINER_CREATE_URL, json={}, status=200)
        with pytest.raises(PermanentError, match="missing id"):
            threads_publisher.publish("threads", {"media_public_url": VIDEO_URL}, CREDENTIALS)


class TestGraphErrorClassification:
    @responses.activate
    def test_http_500_on_container_create_is_transient(self):
        responses.add(responses.POST, _CONTAINER_CREATE_URL, json={"error": {"message": "oops", "code": 2}}, status=500)
        with pytest.raises(TransientError):
            threads_publisher.publish("threads", {"text": "hi"}, CREDENTIALS)

    @responses.activate
    def test_http_429_is_transient(self):
        responses.add(
            responses.POST, _CONTAINER_CREATE_URL, json={"error": {"message": "rate limited", "code": 4}}, status=429
        )
        with pytest.raises(TransientError):
            threads_publisher.publish("threads", {"text": "hi"}, CREDENTIALS)

    @responses.activate
    def test_invalid_token_on_container_create_is_token_expired(self):
        responses.add(
            responses.POST,
            _CONTAINER_CREATE_URL,
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )
        with pytest.raises(TokenExpiredError):
            threads_publisher.publish("threads", {"text": "hi"}, CREDENTIALS)

    @responses.activate
    def test_invalid_token_during_publish_is_token_expired(self):
        responses.add(responses.POST, _CONTAINER_CREATE_URL, json={"id": "creation-7"}, status=200)
        responses.add(responses.GET, _container_status_url("creation-7"), json={"status": "FINISHED"}, status=200)
        responses.add(
            responses.POST,
            _PUBLISH_URL,
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )
        with pytest.raises(TokenExpiredError):
            threads_publisher.publish("threads", {"text": "hi"}, CREDENTIALS)

    @responses.activate
    def test_other_4xx_is_permanent(self):
        responses.add(
            responses.POST, _CONTAINER_CREATE_URL, json={"error": {"message": "Bad request", "code": 100}}, status=400
        )
        with pytest.raises(PermanentError):
            threads_publisher.publish("threads", {"text": "hi"}, CREDENTIALS)


class TestTokenRefresh:
    def test_missing_access_token_is_permanent(self):
        with pytest.raises(PermanentError, match="access_token"):
            threads_publisher.refresh_stored_credentials({})

    @responses.activate
    def test_happy_path_no_rotation(self):
        responses.add(responses.GET, _REFRESH_URL, json={"access_token": "new-access", "expires_in": 5184000}, status=200)

        new_creds = threads_publisher.refresh_stored_credentials(
            {"threads_user_id": THREADS_USER_ID, "username": "acme", "access_token": "old-access"}
        )

        assert new_creds["access_token"] == "new-access"
        assert new_creds["expires_at"] is not None
        # No app secret needed for this call, and threads_user_id/username
        # are carried through unchanged (no rotation concept for either).
        assert new_creds["threads_user_id"] == THREADS_USER_ID
        assert new_creds["username"] == "acme"

        request = responses.calls[0].request
        assert "grant_type=th_refresh_token" in request.url
        assert "access_token=old-access" in request.url

    @responses.activate
    def test_missing_new_access_token_is_transient(self):
        responses.add(responses.GET, _REFRESH_URL, json={"expires_in": 5184000}, status=200)
        with pytest.raises(TransientError, match="did not return a new access_token"):
            threads_publisher.refresh_stored_credentials({"access_token": "old-access"})

    @responses.activate
    def test_invalid_token_is_permanent(self):
        responses.add(
            responses.GET,
            _REFRESH_URL,
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
            status=400,
        )
        with pytest.raises(PermanentError):
            threads_publisher.refresh_stored_credentials({"access_token": "old-access"})

    @responses.activate
    def test_server_error_is_transient(self):
        responses.add(responses.GET, _REFRESH_URL, json={"error": {"message": "oops", "code": 2}}, status=500)
        with pytest.raises(TransientError):
            threads_publisher.refresh_stored_credentials({"access_token": "old-access"})


class TestTokenExpiresWithin:
    def test_missing_expires_at_needs_refresh(self):
        assert threads_publisher.token_expires_within({}, 60) is True

    def test_unparseable_expires_at_needs_refresh(self):
        assert threads_publisher.token_expires_within({"expires_at": "not-a-date"}, 60) is True

    def test_far_future_expiry_does_not_need_refresh(self):
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        assert threads_publisher.token_expires_within({"expires_at": future}, 60) is False

    def test_near_expiry_needs_refresh(self):
        from datetime import datetime, timedelta, timezone

        soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        assert threads_publisher.token_expires_within({"expires_at": soon}, 60) is True


class TestWebOAuthFlow:
    """Phase 29f — build_authorization_url/exchange_code_for_credentials, the
    in-browser counterpart to a CLI authorize script (Threads has none, same
    as Pinterest — this is its only authorization flow). Modeled on
    tests/test_publisher_pinterest.py::TestWebOAuthFlow, minus every PKCE-
    specific assertion (Threads' OAuth dialog needs none)."""

    def test_build_authorization_url_missing_app_id_raises(self, monkeypatch):
        monkeypatch.delenv("THREADS_APP_ID", raising=False)
        with pytest.raises(PermanentError):
            threads_publisher.build_authorization_url("http://localhost/callback", "state123")

    def test_build_authorization_url_happy_path(self):
        url = threads_publisher.build_authorization_url("http://localhost/callback", "state123")

        assert url.startswith(threads_publisher.AUTHORIZE_URL + "?")
        assert "client_id=app-id-1" in url
        assert "state=state123" in url
        assert "response_type=code" in url
        assert "code_challenge" not in url

    def test_exchange_code_for_credentials_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("THREADS_APP_ID", raising=False)
        monkeypatch.delenv("THREADS_APP_SECRET", raising=False)
        with pytest.raises(PermanentError):
            threads_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback")

    @responses.activate
    def test_exchange_code_for_credentials_happy_path(self):
        responses.add(responses.POST, _TOKEN_URL, json={"access_token": "short-lived-tok", "user_id": "ignored"}, status=200)
        responses.add(
            responses.GET,
            _LONG_LIVED_EXCHANGE_URL,
            json={"access_token": "long-lived-tok", "expires_in": 5184000},
            status=200,
        )
        responses.add(responses.GET, _ME_URL, json={"id": THREADS_USER_ID, "username": "acme"}, status=200)

        creds = threads_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback")

        assert creds == {
            "threads_user_id": THREADS_USER_ID,
            "username": "acme",
            "access_token": "long-lived-tok",
            "expires_at": creds["expires_at"],
        }
        assert creds["expires_at"] is not None

        short_lived_request = responses.calls[0].request
        assert "grant_type=authorization_code" in short_lived_request.body
        assert "code=auth-code" in short_lived_request.body

        long_lived_request = responses.calls[1].request
        assert "grant_type=th_exchange_token" in long_lived_request.url
        assert "access_token=short-lived-tok" in long_lived_request.url

    @responses.activate
    def test_exchange_code_for_credentials_missing_short_lived_token_is_permanent(self):
        responses.add(responses.POST, _TOKEN_URL, json={"user_id": "ignored"}, status=200)
        with pytest.raises(PermanentError):
            threads_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback")

    @responses.activate
    def test_exchange_code_for_credentials_wraps_token_endpoint_failure_as_permanent(self):
        responses.add(
            responses.POST,
            _TOKEN_URL,
            json={"error": {"message": "This authorization code has been used.", "code": 100}},
            status=400,
        )
        with pytest.raises(PermanentError, match="authorization code has been used"):
            threads_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback")

    @responses.activate
    def test_exchange_code_for_credentials_missing_profile_id_is_permanent(self):
        responses.add(responses.POST, _TOKEN_URL, json={"access_token": "short-lived-tok"}, status=200)
        responses.add(
            responses.GET, _LONG_LIVED_EXCHANGE_URL, json={"access_token": "long-lived-tok", "expires_in": 5184000}, status=200
        )
        responses.add(responses.GET, _ME_URL, json={"username": "acme"}, status=200)

        with pytest.raises(PermanentError):
            threads_publisher.exchange_code_for_credentials("auth-code", "http://localhost/callback")
