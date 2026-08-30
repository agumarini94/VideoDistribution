"""
Tests for Phase 17's additions to app/publishers/twitter.py: chunked media
upload (v1.1 upload.twitter.com, INIT -> APPEND -> FINALIZE -> STATUS),
media-cap pre-flight validation, and thread posting (sequential replies,
whole-thread pre-flight validation, partial-thread failure reporting). All
HTTP is mocked with `responses` — nothing here talks to the real X API.

See tests/test_publisher_twitter.py for the pre-existing single-tweet /
credential-resolution / 280-char-guard coverage, unchanged by this phase.
"""

import pytest
import responses

from app.exceptions import PermanentError, TransientError
from app.publishers import twitter as twitter_publisher

_TWEETS_URL = "https://api.twitter.com/2/tweets"
_MEDIA_URL = "https://upload.twitter.com/1.1/media/upload.json"

ACCOUNT_CREDENTIALS = {"access_token": "acc-token", "access_token_secret": "acc-secret"}


@pytest.fixture(autouse=True)
def _app_credentials(monkeypatch):
    monkeypatch.setenv("X_API_KEY", "app-key")
    monkeypatch.setenv("X_API_SECRET", "app-secret")
    monkeypatch.delenv("X_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("X_ACCESS_TOKEN_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # _wait_for_processing sleeps between STATUS polls; tests don't want to
    # actually wait, and check_after_secs is set to 0 below anyway.
    monkeypatch.setattr(twitter_publisher.time, "sleep", lambda _seconds: None)


@pytest.fixture
def image_file(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake png bytes")
    return path


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"0123456789ABCDEF")  # 16 bytes -> 2 chunks when chunk size is patched to 8
    return path


def _tweet_response(tweet_id="1"):
    return {"data": {"id": tweet_id, "text": "irrelevant"}}


def _mock_tweet(tweet_id="1"):
    responses.add(responses.POST, _TWEETS_URL, json=_tweet_response(tweet_id), status=201)


def _mock_media_upload(media_id="111", append_calls=1, processing_states=None):
    """
    Registers INIT -> APPEND (x append_calls) -> FINALIZE -> optional STATUS
    poll(s), all against the same upload.twitter.com endpoint (tweepy.API
    distinguishes them by the "command" field, `responses` just replays
    registrations in order per method+URL).
    """
    responses.add(responses.POST, _MEDIA_URL, json={"media_id": media_id}, status=202)  # INIT
    for _ in range(append_calls):
        responses.add(responses.POST, _MEDIA_URL, status=204)  # APPEND (empty body)

    finalize_body = {"media_id": media_id}
    if processing_states:
        finalize_body["processing_info"] = processing_states[0]
    responses.add(responses.POST, _MEDIA_URL, json=finalize_body, status=201)  # FINALIZE

    for state in (processing_states or [])[1:]:
        responses.add(responses.GET, _MEDIA_URL, json={"media_id": media_id, "processing_info": state}, status=200)


def _media_calls():
    return [c for c in responses.calls if c.request.url.startswith(_MEDIA_URL)]


class TestMediaCapValidation:
    @responses.activate
    def test_five_images_rejected_without_any_http_call(self, tmp_path):
        paths = []
        for i in range(5):
            p = tmp_path / f"img{i}.png"
            p.write_bytes(b"\x89PNG fake")
            paths.append(str(p))

        with pytest.raises(PermanentError, match="4"):
            twitter_publisher.publish("twitter", {"text": "hi", "media_paths": paths}, ACCOUNT_CREDENTIALS)

        assert len(responses.calls) == 0

    @responses.activate
    def test_two_videos_rejected_without_any_http_call(self, tmp_path):
        paths = []
        for i in range(2):
            p = tmp_path / f"clip{i}.mp4"
            p.write_bytes(b"fake mp4")
            paths.append(str(p))

        with pytest.raises(PermanentError, match="1 video"):
            twitter_publisher.publish("twitter", {"text": "hi", "media_paths": paths}, ACCOUNT_CREDENTIALS)

        assert len(responses.calls) == 0

    @responses.activate
    def test_mixed_image_and_video_rejected_without_any_http_call(self, tmp_path, image_file, video_file):
        with pytest.raises(PermanentError, match="mix"):
            twitter_publisher.publish(
                "twitter",
                {"text": "hi", "media_paths": [str(image_file), str(video_file)]},
                ACCOUNT_CREDENTIALS,
            )

        assert len(responses.calls) == 0

    @responses.activate
    def test_missing_media_file_rejected_without_any_http_call(self, tmp_path):
        missing = tmp_path / "does-not-exist.png"

        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {"text": "hi", "media_paths": [str(missing)]}, ACCOUNT_CREDENTIALS)

        assert len(responses.calls) == 0


class TestChunkedMediaUpload:
    @responses.activate
    def test_video_happy_path_polls_status_to_succeeded(self, video_file, monkeypatch):
        monkeypatch.setattr(twitter_publisher, "_MEDIA_UPLOAD_CHUNK_SIZE", 8)
        _mock_media_upload(
            media_id="222",
            append_calls=2,
            processing_states=[
                {"state": "in_progress", "check_after_secs": 0},
                {"state": "succeeded"},
            ],
        )
        _mock_tweet("999")

        result = twitter_publisher.publish(
            "twitter", {"text": "check out this clip", "media_paths": [str(video_file)]}, ACCOUNT_CREDENTIALS
        )

        assert result == {"platform": "twitter", "external_id": "999"}
        media_calls = _media_calls()
        assert len(media_calls) == 5  # INIT + 2 APPEND + FINALIZE + 1 STATUS poll

        tweet_call = [c for c in responses.calls if c.request.url == _TWEETS_URL][0]
        assert '"media_ids": ["222"]' in tweet_call.request.body.decode()

    @responses.activate
    def test_image_happy_path_skips_status_polling(self, image_file):
        # Static images finalize synchronously (no processing_info), so no
        # GET .../media/upload.json?command=STATUS call should ever happen.
        _mock_media_upload(media_id="333", append_calls=1, processing_states=None)
        _mock_tweet("1")

        result = twitter_publisher.publish(
            "twitter", {"text": "a photo", "media_paths": [str(image_file)]}, ACCOUNT_CREDENTIALS
        )

        assert result == {"platform": "twitter", "external_id": "1"}
        media_calls = _media_calls()
        assert len(media_calls) == 3  # INIT + APPEND + FINALIZE, no STATUS
        assert all(c.request.method == "POST" for c in media_calls)

    @responses.activate
    def test_failed_processing_raises_permanent_error_with_reason(self, video_file, monkeypatch):
        monkeypatch.setattr(twitter_publisher, "_MEDIA_UPLOAD_CHUNK_SIZE", 8)
        _mock_media_upload(
            media_id="444",
            append_calls=2,
            processing_states=[
                {"state": "in_progress", "check_after_secs": 0},
                {"state": "failed", "error": {"message": "invalid video format"}},
            ],
        )
        # No tweet mock registered: if the code somehow tried to post anyway,
        # this would raise a connection error instead of masking the bug.

        with pytest.raises(PermanentError, match="invalid video format"):
            twitter_publisher.publish(
                "twitter", {"text": "check out this clip", "media_paths": [str(video_file)]}, ACCOUNT_CREDENTIALS
            )

        assert not any(c.request.url == _TWEETS_URL for c in responses.calls)

    @responses.activate
    def test_media_init_http_500_is_transient(self, image_file):
        responses.add(responses.POST, _MEDIA_URL, status=500)

        with pytest.raises(TransientError):
            twitter_publisher.publish("twitter", {"text": "hi", "media_paths": [str(image_file)]}, ACCOUNT_CREDENTIALS)


class TestThreadValidation:
    @responses.activate
    def test_text_and_thread_together_is_permanent(self):
        with pytest.raises(PermanentError, match="mutually exclusive"):
            twitter_publisher.publish(
                "twitter", {"text": "hi", "thread": [{"text": "hi"}]}, ACCOUNT_CREDENTIALS
            )
        assert len(responses.calls) == 0

    @responses.activate
    def test_neither_text_nor_thread_is_permanent(self):
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {}, ACCOUNT_CREDENTIALS)
        assert len(responses.calls) == 0

    @responses.activate
    def test_empty_thread_is_permanent(self):
        with pytest.raises(PermanentError):
            twitter_publisher.publish("twitter", {"thread": []}, ACCOUNT_CREDENTIALS)
        assert len(responses.calls) == 0

    @responses.activate
    def test_over_280_chars_anywhere_in_thread_posts_nothing(self):
        # No responses registered at all: if any tweet in the thread got
        # posted despite the bad one, this would raise a connection error.
        thread = [{"text": "first"}, {"text": "second"}, {"text": "x" * 281}]

        with pytest.raises(PermanentError, match="281"):
            twitter_publisher.publish("twitter", {"thread": thread}, ACCOUNT_CREDENTIALS)

        assert len(responses.calls) == 0


class TestThreadHappyPath:
    @responses.activate
    def test_three_tweets_chained_via_in_reply_to(self):
        _mock_tweet("100")
        _mock_tweet("101")
        _mock_tweet("102")

        thread = [{"text": "one"}, {"text": "two"}, {"text": "three"}]
        result = twitter_publisher.publish("twitter", {"thread": thread}, ACCOUNT_CREDENTIALS)

        assert result == {"platform": "twitter", "external_id": "100", "tweet_ids": ["100", "101", "102"]}

        tweet_calls = [c for c in responses.calls if c.request.url == _TWEETS_URL]
        assert len(tweet_calls) == 3
        bodies = [c.request.body.decode() for c in tweet_calls]
        assert '"reply"' not in bodies[0]
        assert '"in_reply_to_tweet_id": "100"' in bodies[1]
        assert '"in_reply_to_tweet_id": "101"' in bodies[2]

    @responses.activate
    def test_thread_tweet_with_media_attaches_media_ids(self, image_file):
        _mock_media_upload(media_id="555", append_calls=1, processing_states=None)
        _mock_tweet("200")

        thread = [{"text": "one", "media_paths": [str(image_file)]}]
        result = twitter_publisher.publish("twitter", {"thread": thread}, ACCOUNT_CREDENTIALS)

        assert result == {"platform": "twitter", "external_id": "200", "tweet_ids": ["200"]}
        tweet_call = [c for c in responses.calls if c.request.url == _TWEETS_URL][0]
        assert '"media_ids": ["555"]' in tweet_call.request.body.decode()


class TestThreadMidFailure:
    @responses.activate
    def test_mid_thread_failure_reports_posted_count_and_last_id(self):
        _mock_tweet("100")
        responses.add(responses.POST, _TWEETS_URL, status=500)  # second tweet fails

        thread = [{"text": "one"}, {"text": "two"}, {"text": "three"}]
        with pytest.raises(TransientError) as exc_info:
            twitter_publisher.publish("twitter", {"thread": thread}, ACCOUNT_CREDENTIALS)

        message = str(exc_info.value)
        assert "tweet 2/3" in message
        assert "posting 1 tweet" in message
        assert "100" in message

        # The third tweet must never have been attempted.
        tweet_calls = [c for c in responses.calls if c.request.url == _TWEETS_URL]
        assert len(tweet_calls) == 2

    @responses.activate
    def test_first_tweet_permanent_failure_reports_zero_posted(self):
        responses.add(responses.POST, _TWEETS_URL, status=400)

        thread = [{"text": "one"}, {"text": "two"}]
        with pytest.raises(PermanentError) as exc_info:
            twitter_publisher.publish("twitter", {"thread": thread}, ACCOUNT_CREDENTIALS)

        message = str(exc_info.value)
        assert "tweet 1/2" in message
        assert "posting 0 tweet" in message
        assert "last successful tweet id: None" in message
