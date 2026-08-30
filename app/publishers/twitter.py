"""
Real Twitter/X publisher (X API v2, POST /2/tweets via tweepy, OAuth 1.0a
user-context signing).

Same contract as the other publishers in this package: publish() is a pure
function that knows nothing about Celery or the database. It either returns
a result dict or raises TransientError / PermanentError. This is the first
publisher to take a third argument (account_credentials) — the start of
multi-account support (Phase 6, see app/models.py Account). app/tasks.py
resolves the Account row and passes its `credentials` JSON here as a plain
dict, so this function still never touches the database.

Credentials resolution:
  - App-level (consumer) key/secret: always from env vars X_API_KEY /
    X_API_SECRET, since these belong to the app registered on the X
    Developer Portal, not to any individual account.
  - Per-account access token: from account_credentials (the job's
    Account.credentials JSON), keys "access_token" / "access_token_secret"
    — see scripts/add_account.py.
  - If the job has no account (account_credentials is None): falls back to
    env vars X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET, for single-account
    setups that don't need an Account row yet.

Credentials aren't provided by the client yet, so every code path that
depends on them raises a clear PermanentError instead of crashing, and
nothing here executes at import time.

Payload contract (Phase 17 extends this, backwards compatible):
  - "text" alone -> a single tweet, as before.
  - optional "media_paths": local file paths to attach (X caps: 4 images or
    1 video, never mixed — validated pre-flight, before any upload starts).
  - optional "thread": an ordered list of {"text", optional "media_paths"}
    dicts, posted sequentially, each reply chained to the previous via
    in_reply_to_tweet_id. Every tweet's text (and media caps) is validated
    up front, before tweet #1 is posted, so a validation error we could
    have caught never leaves a half-posted thread.
  - "text" and "thread" are mutually exclusive: exactly one must be present.
  - Result dict: external_id is the first tweet's id; threads additionally
    return "tweet_ids" (every tweet's id, in order).

Media upload (Phase 17): X's media upload lives on a different host/API
version (v1.1, upload.twitter.com) than tweet creation (v2). tweepy.API
(OAuth1UserHandler) is reused here purely for its already-correct OAuth 1.0a
signing and chunked-upload endpoints (chunked_upload_init/_append/_finalize/
get_media_upload_status) — this module still drives the chunking, media
category selection and processing-status polling itself, rather than using
tweepy's own higher-level chunked_upload() helper, so failed async
processing (video/gif) can be classified and reported like every other
error here.
"""

import mimetypes
import os
import time
from pathlib import Path

import tweepy

from app.exceptions import PermanentError, PublishError, TransientError

_MAX_TWEET_LENGTH = 280

_MEDIA_UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB, safely under X's ~5 MiB chunk cap
_MAX_IMAGES_PER_TWEET = 4
_MAX_VIDEOS_PER_TWEET = 1
_MEDIA_CATEGORY_BY_KIND = {"image": "tweet_image", "gif": "tweet_gif", "video": "tweet_video"}


def publish(platform: str, payload: dict, account_credentials: dict | None = None) -> dict:
    """
    Posts to X: a single tweet (payload["text"], optionally with
    payload["media_paths"]) or a thread (payload["thread"]) — see the module
    docstring for the full payload contract.
    """
    try:
        _validate_top_level_payload(payload)
        creds = _resolve_credentials(account_credentials)

        if "thread" in payload:
            return _publish_thread(payload["thread"], creds)
        return _publish_single(payload, creds)
    except (TransientError, PermanentError):
        raise
    except tweepy.HTTPException as exc:
        raise _classify_http_error(exc) from exc
    except tweepy.TweepyException as exc:
        # Client-side failures (bad auth object, connection errors tweepy
        # wraps itself, etc.) rather than an HTTP response we can classify.
        raise TransientError(f"Unexpected X API client error: {exc}") from exc
    except Exception as exc:  # normalize anything unexpected per the publisher contract
        raise TransientError(f"Unexpected error talking to the X API: {exc}") from exc


def _validate_top_level_payload(payload: dict) -> None:
    has_text = bool(payload.get("text"))
    has_thread = bool(payload.get("thread"))
    if has_text and has_thread:
        raise PermanentError("Payload cannot include both 'text' and 'thread' — they are mutually exclusive")
    if not has_text and not has_thread:
        raise PermanentError("Payload must include either 'text' or 'thread'")


def _publish_single(payload: dict, creds: dict) -> dict:
    text = payload["text"]
    _validate_text(text)

    media_paths = payload.get("media_paths")
    if media_paths:
        _validate_media_paths(media_paths)

    client = _build_client(creds)
    media_ids = None
    if media_paths:
        media_ids = _upload_media(_build_api_v1(creds), media_paths)

    tweet_id = _post_tweet(client, text, media_ids=media_ids)
    return {"platform": "twitter", "external_id": tweet_id}


def _publish_thread(tweets: list, creds: dict) -> dict:
    _validate_thread(tweets)

    client = _build_client(creds)
    api_v1 = None  # built lazily: only needed if some tweet in the thread has media

    tweet_ids: list[str] = []
    previous_id = None
    for index, tweet in enumerate(tweets):
        try:
            media_paths = tweet.get("media_paths")
            media_ids = None
            if media_paths:
                if api_v1 is None:
                    api_v1 = _build_api_v1(creds)
                media_ids = _upload_media(api_v1, media_paths, label=f"thread[{index}].media_paths")
            previous_id = _post_tweet(client, tweet["text"], media_ids=media_ids, in_reply_to_tweet_id=previous_id)
            tweet_ids.append(previous_id)
        except (TransientError, PermanentError) as exc:
            posted = len(tweet_ids)
            last_id = tweet_ids[-1] if tweet_ids else None
            raise type(exc)(
                f"Thread posting failed at tweet {index + 1}/{len(tweets)} after posting {posted} "
                f"tweet(s) (last successful tweet id: {last_id}): {exc}"
            ) from exc

    return {"platform": "twitter", "external_id": tweet_ids[0], "tweet_ids": tweet_ids}


def _validate_text(text, label: str = "text") -> None:
    if not text:
        raise PermanentError(f"Missing required payload field: {label}")

    # Simple len() count. X actually counts each URL as a fixed 23 chars
    # (via its t.co wrapper) regardless of the URL's real length, so a
    # tweet whose text is mostly a very long URL could pass this check and
    # still get rejected upstream as too long — acceptable for now, this is
    # a cheap pre-flight guard, not a reimplementation of X's counting rules.
    length = len(text)
    if length > _MAX_TWEET_LENGTH:
        raise PermanentError(f"{label} exceeds X's {_MAX_TWEET_LENGTH} character limit ({length} characters)")


def _validate_thread(tweets) -> None:
    if not isinstance(tweets, list) or not tweets:
        raise PermanentError("'thread' must be a non-empty list of tweets")
    for index, tweet in enumerate(tweets):
        if not isinstance(tweet, dict):
            raise PermanentError(f"thread[{index}] must be an object with a 'text' field")
        _validate_text(tweet.get("text"), label=f"thread[{index}].text")
        media_paths = tweet.get("media_paths")
        if media_paths:
            _validate_media_paths(media_paths, label=f"thread[{index}].media_paths")


def _media_kind(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime == "image/gif":
        return "gif"
    if mime is not None and mime.startswith("image/"):
        return "image"
    if mime is not None and mime.startswith("video/"):
        return "video"
    raise PermanentError(f"Unsupported or undetectable media type for {path} (guessed mime type: {mime})")


def _validate_media_paths(media_paths, label: str = "media_paths") -> list[tuple[Path, str]]:
    if not media_paths:
        raise PermanentError(f"{label} must be a non-empty list of file paths")

    resolved = []
    for raw_path in media_paths:
        path = Path(raw_path)
        if not path.is_file():
            raise PermanentError(f"Media file not found: {path}")
        resolved.append((path, _media_kind(path)))

    video_count = sum(1 for _, kind in resolved if kind == "video")
    image_count = sum(1 for _, kind in resolved if kind in ("image", "gif"))

    if video_count and image_count:
        raise PermanentError(f"{label}: cannot mix images and video in the same tweet")
    if video_count > _MAX_VIDEOS_PER_TWEET:
        raise PermanentError(f"{label}: X allows at most {_MAX_VIDEOS_PER_TWEET} video per tweet, got {video_count}")
    if image_count > _MAX_IMAGES_PER_TWEET:
        raise PermanentError(f"{label}: X allows at most {_MAX_IMAGES_PER_TWEET} images per tweet, got {image_count}")

    return resolved


def _upload_media(api_v1: tweepy.API, media_paths, label: str = "media_paths") -> list[str]:
    resolved = _validate_media_paths(media_paths, label=label)
    return [_upload_one_media(api_v1, path, kind) for path, kind in resolved]


def _upload_one_media(api_v1: tweepy.API, path: Path, kind: str) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    total_bytes = path.stat().st_size
    media_category = _MEDIA_CATEGORY_BY_KIND[kind]

    try:
        media = api_v1.chunked_upload_init(total_bytes, mime, media_category=media_category)
        media_id = media.media_id

        with path.open("rb") as f:
            segment_index = 0
            while True:
                chunk = f.read(_MEDIA_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                api_v1.chunked_upload_append(media_id, (path.name, chunk), segment_index)
                segment_index += 1

        media = api_v1.chunked_upload_finalize(media_id)
        _wait_for_processing(api_v1, media)
    except (TransientError, PermanentError):
        raise
    except tweepy.HTTPException as exc:
        raise _classify_http_error(exc) from exc
    except tweepy.TweepyException as exc:
        raise TransientError(f"Unexpected X API client error uploading media {path}: {exc}") from exc

    return str(media_id)


def _wait_for_processing(api_v1: tweepy.API, media) -> None:
    """
    Polls GET .../media/upload.json?command=STATUS while X asynchronously
    processes video/gif uploads (processing_info.state: pending ->
    in_progress -> succeeded/failed). Static images finalize synchronously
    and carry no processing_info at all, so this is a no-op for them.
    """
    processing_info = getattr(media, "processing_info", None)
    while processing_info and processing_info.get("state") in ("pending", "in_progress"):
        time.sleep(processing_info.get("check_after_secs", 1))
        media = api_v1.get_media_upload_status(media.media_id)
        processing_info = getattr(media, "processing_info", None)

    if processing_info and processing_info.get("state") == "failed":
        error = processing_info.get("error") or {}
        reason = error.get("message") or error.get("name") or "unknown reason"
        raise PermanentError(f"X rejected media during processing: {reason}")


def _post_tweet(client: tweepy.Client, text: str, media_ids=None, in_reply_to_tweet_id=None) -> str:
    kwargs = {"text": text}
    if media_ids:
        kwargs["media_ids"] = media_ids
    if in_reply_to_tweet_id:
        kwargs["in_reply_to_tweet_id"] = in_reply_to_tweet_id

    try:
        response = client.create_tweet(**kwargs)
    except (TransientError, PermanentError):
        raise
    except tweepy.HTTPException as exc:
        raise _classify_http_error(exc) from exc
    except tweepy.TweepyException as exc:
        raise TransientError(f"Unexpected X API client error: {exc}") from exc

    return response.data["id"]


def _resolve_credentials(account_credentials: dict | None) -> dict:
    api_key = os.getenv("X_API_KEY", "").strip()
    api_secret = os.getenv("X_API_SECRET", "").strip()
    missing_app = [name for name, value in (("X_API_KEY", api_key), ("X_API_SECRET", api_secret)) if not value]
    if missing_app:
        raise PermanentError(
            "X (Twitter) app credentials are not configured (missing: "
            f"{', '.join(missing_app)}). Set them in .env."
        )

    if account_credentials is not None:
        access_token = str(account_credentials.get("access_token", "")).strip()
        access_token_secret = str(account_credentials.get("access_token_secret", "")).strip()
        source = "the job's Account credentials"
    else:
        access_token = os.getenv("X_ACCESS_TOKEN", "").strip()
        access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET", "").strip()
        source = "env vars (this job has no account_id)"

    missing_token = [
        name
        for name, value in (("access_token", access_token), ("access_token_secret", access_token_secret))
        if not value
    ]
    if missing_token:
        raise PermanentError(
            f"X (Twitter) access token credentials are missing from {source}: {', '.join(missing_token)}."
        )

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "access_token": access_token,
        "access_token_secret": access_token_secret,
    }


def _build_client(creds: dict) -> tweepy.Client:
    return tweepy.Client(
        consumer_key=creds["api_key"],
        consumer_secret=creds["api_secret"],
        access_token=creds["access_token"],
        access_token_secret=creds["access_token_secret"],
    )


def _build_api_v1(creds: dict) -> tweepy.API:
    auth = tweepy.OAuth1UserHandler(
        creds["api_key"], creds["api_secret"], creds["access_token"], creds["access_token_secret"]
    )
    return tweepy.API(auth)


def _classify_http_error(exc: tweepy.HTTPException) -> PublishError:
    status = exc.response.status_code if exc.response is not None else None

    if status == 429 or (status is not None and status >= 500):
        return TransientError(f"X API transient error (HTTP {status}): {exc}")

    if status in (401, 403):
        return PermanentError(f"X API rejected the credentials (HTTP {status}): {exc}")

    return PermanentError(f"X API rejected the request (HTTP {status}): {exc}")
