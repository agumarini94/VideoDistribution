"""
Real Twitter/X publisher (X API v2, POST /2/tweets, OAuth 2.0 user-context
Bearer auth).

Same contract as the other publishers in this package: publish() is a pure
function that knows nothing about Celery or the database. It either returns
a result dict or raises TransientError / PermanentError (or the
TokenExpiredError subclass, see below). app/tasks.py resolves the Account
row and passes its `credentials` JSON here as a plain dict, so this function
still never touches the database.

Phase 21 migration (from OAuth 1.0a + v1.1 media upload): X's developer
console now only issues OAuth 2.0 user tokens (scopes: tweet.read,
tweet.write, users.read, offline.access). Access tokens expire in ~2 hours;
refresh tokens are SINGLE-USE and ROTATE on every refresh (a new
access_token AND a new refresh_token are returned, invalidating the old
refresh_token). Our app is a confidential client, so refresh requests
authenticate with client_id/client_secret via HTTP Basic auth. All of this
means:
  - Every API call (tweet creation, media upload) is a plain
    `Authorization: Bearer <access_token>` request — no per-request request
    signing like OAuth 1.0a needed, so tweepy.Client(bearer_token=...) is
    enough for tweet creation; media upload is implemented directly with
    `requests` (tweepy's chunked-upload helpers only cover the v1.1
    upload.twitter.com host/auth, not the v2 endpoint used here).
  - A publisher can't safely refresh its own token: the refresh must be
    persisted (both new tokens) BEFORE the new access_token is used again,
    or a crash between refresh and persist strands the account (old token
    now invalid, new refresh_token never saved). Publishers are pure and
    can't write to the database, so publish() never refreshes anything
    itself — instead, an expired/invalid token surfaces as a distinct
    TokenExpiredError (see app/exceptions.py), and app/tasks.py::publish_job
    is the one place that refreshes, persists, and retries once. See
    refresh_stored_credentials()/token_expires_within() below for the
    (proactive, Beat-driven) and reactive halves of that flow.

Credentials resolution — account_credentials dict keys: client_id,
client_secret, access_token, refresh_token (+ optionally expires_at, only
meaningful together with token_expires_within/refresh_stored_credentials,
not read by publish() itself). All four are per-account (Phase 21 changes
this from Phase 6's app-level-env / per-account-token split, since a
confidential client's refresh flow needs client_id+secret alongside
whichever refresh_token they're paired with):
  - Job has an account_id: all four come from account_credentials (the
    job's Account.credentials JSON) — see scripts/add_account.py.
  - Job has no account_id: falls back to env vars TWITTER_CLIENT_ID /
    TWITTER_CLIENT_SECRET / TWITTER_ACCESS_TOKEN / TWITTER_REFRESH_TOKEN,
    for single-account setups that don't need an Account row yet. Note:
    since refresh tokens rotate on every use, single-account/env-var mode
    can't persist a rotated refresh_token anywhere — it only survives one
    reactive refresh (app/tasks.py logs a warning and treats
    TokenExpiredError as a plain transient error for env-var-only jobs
    instead of attempting a refresh it can't save). Real accounts should
    get an Account row once OAuth 2.0 credentials exist.

Credentials aren't provided by the client yet (X Developer Portal account
still being created), so every code path that depends on them raises a
clear PermanentError instead of crashing, and nothing here executes at
import time. The v2 media upload endpoint details below come from
docs.x.com (provided directly, not guessed) but are, like every other
publisher in this package before real credentials arrive, untested against
a live account.

Payload contract (unchanged since Phase 17):
  - "text" alone -> a single tweet.
  - optional "media_paths": local file paths to attach (X caps: 4 images or
    1 video, never mixed — validated pre-flight, before any upload starts).
  - optional "thread": an ordered list of {"text", optional "media_paths"}
    dicts, posted sequentially, each reply chained to the previous via
    in_reply_to_tweet_id. Every tweet's text (280-char guard) and media caps
    are validated up front, before tweet #1 is posted, so a validation
    error we could have caught never leaves a half-posted thread.
  - "text" and "thread" are mutually exclusive: exactly one must be present.
  - Result dict: external_id is the first tweet's id; threads additionally
    return "tweet_ids" (every tweet's id, in order).

Media upload (Phase 21, replaces Phase 17's v1.1 upload.twitter.com flow):
single endpoint https://api.x.com/2/media/upload, multipart/form-data,
Bearer auth, command=INIT|APPEND|FINALIZE + a GET ...?command=STATUS poll —
same INIT/APPEND/FINALIZE/STATUS shape as v1.1 conceptually, different host
and request encoding. media_category stays "tweet_image"/"tweet_gif"/
"tweet_video" (unchanged). Attaching the uploaded media_id to a tweet is
unchanged: tweepy's create_tweet(media_ids=[...]) already sends it nested
as {"media": {"media_ids": [...]}} in the v2 tweet body, which is the shape
docs.x.com documents for POST /2/tweets.
"""

import mimetypes
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import tweepy

from app.exceptions import PermanentError, PublishError, TokenExpiredError, TransientError

_MAX_TWEET_LENGTH = 280

_MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"
_MEDIA_UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB, within docs.x.com's ~1-4 MB per-chunk guidance
_MAX_IMAGES_PER_TWEET = 4
_MAX_VIDEOS_PER_TWEET = 1
_MEDIA_CATEGORY_BY_KIND = {"image": "tweet_image", "gif": "tweet_gif", "video": "tweet_video"}

# X's OAuth 2.0 token endpoint (grant_type=refresh_token), used only by
# refresh_stored_credentials() below — never by publish() itself. This is
# the long-documented, stable OAuth 2.0 Authorization Code + PKCE token
# endpoint (unlike the v2 media upload URL above, this one wasn't pasted
# from docs.x.com directly — flagged here since it's likewise unverified
# against a live account yet).
_TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}

# error codes from the OAuth 2.0 token endpoint worth retrying (transient on
# X's side); everything else (e.g. invalid_grant for a revoked/expired or
# already-used refresh token) is permanent. Same convention as
# app/publishers/tiktok.py's _TRANSIENT_TOKEN_ERROR_CODES.
_TRANSIENT_TOKEN_ERROR_CODES = {"server_error", "temporarily_unavailable"}

_CREDENTIAL_FIELDS = ("client_id", "client_secret", "access_token", "refresh_token")
_ENV_VAR_BY_FIELD = {
    "client_id": "TWITTER_CLIENT_ID",
    "client_secret": "TWITTER_CLIENT_SECRET",
    "access_token": "TWITTER_ACCESS_TOKEN",
    "refresh_token": "TWITTER_REFRESH_TOKEN",
}


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
        media_ids = _upload_media(creds["access_token"], media_paths)

    tweet_id = _post_tweet(client, text, media_ids=media_ids)
    return {"platform": "twitter", "external_id": tweet_id}


def _publish_thread(tweets: list, creds: dict) -> dict:
    _validate_thread(tweets)

    client = _build_client(creds)

    tweet_ids: list[str] = []
    previous_id = None
    for index, tweet in enumerate(tweets):
        try:
            media_paths = tweet.get("media_paths")
            media_ids = None
            if media_paths:
                media_ids = _upload_media(creds["access_token"], media_paths, label=f"thread[{index}].media_paths")
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


def _upload_media(access_token: str, media_paths, label: str = "media_paths") -> list[str]:
    resolved = _validate_media_paths(media_paths, label=label)
    return [_upload_one_media(access_token, path, kind) for path, kind in resolved]


def _upload_one_media(access_token: str, path: Path, kind: str) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    total_bytes = path.stat().st_size
    media_category = _MEDIA_CATEGORY_BY_KIND[kind]

    try:
        media_id = _media_init(access_token, mime, total_bytes, media_category)

        with path.open("rb") as f:
            segment_index = 0
            while True:
                chunk = f.read(_MEDIA_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                _media_append(access_token, media_id, segment_index, path.name, chunk)
                segment_index += 1

        processing_info = _media_finalize(access_token, media_id)
        _wait_for_processing(access_token, media_id, processing_info)
    except (TransientError, PermanentError):
        raise
    except requests.RequestException as exc:
        raise TransientError(f"Network error uploading media {path} to X: {exc}") from exc

    return media_id


def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _multipart_fields(fields: dict) -> dict:
    # requests only encodes a request as multipart/form-data when the
    # `files` argument is used — wrapping plain string fields as (None,
    # value) forces multipart encoding for them too, which is what
    # docs.x.com specifies for INIT/APPEND/FINALIZE even for fields that
    # carry no binary data.
    return {key: (None, str(value)) for key, value in fields.items()}


def _media_init(access_token: str, mime: str | None, total_bytes: int, media_category: str) -> str:
    fields = _multipart_fields(
        {"command": "INIT", "media_type": mime or "application/octet-stream", "total_bytes": total_bytes, "media_category": media_category}
    )
    response = requests.post(_MEDIA_UPLOAD_URL, headers=_auth_headers(access_token), files=fields, timeout=30)
    body = _raise_for_media_error(response, "initializing media upload")

    media_id = (body.get("data") or {}).get("id")
    if not media_id:
        raise PermanentError(f"X media INIT response is missing data.id: {body}")
    return str(media_id)


def _media_append(access_token: str, media_id: str, segment_index: int, filename: str, chunk: bytes) -> None:
    fields = _multipart_fields({"command": "APPEND", "media_id": media_id, "segment_index": segment_index})
    files = {**fields, "media": (filename, chunk, "application/octet-stream")}
    response = requests.post(_MEDIA_UPLOAD_URL, headers=_auth_headers(access_token), files=files, timeout=120)
    _raise_for_media_error(response, f"uploading media chunk {segment_index}")


def _media_finalize(access_token: str, media_id: str) -> dict | None:
    fields = _multipart_fields({"command": "FINALIZE", "media_id": media_id})
    response = requests.post(_MEDIA_UPLOAD_URL, headers=_auth_headers(access_token), files=fields, timeout=30)
    body = _raise_for_media_error(response, "finalizing media upload")
    return (body.get("data") or {}).get("processing_info")


def _media_status(access_token: str, media_id: str) -> dict | None:
    response = requests.get(
        _MEDIA_UPLOAD_URL,
        headers=_auth_headers(access_token),
        params={"command": "STATUS", "media_id": media_id},
        timeout=30,
    )
    body = _raise_for_media_error(response, "polling media status")
    return (body.get("data") or {}).get("processing_info")


def _wait_for_processing(access_token: str, media_id: str, processing_info: dict | None) -> None:
    """
    Polls GET .../2/media/upload?command=STATUS while X asynchronously
    processes video/gif uploads (processing_info.state: pending ->
    in_progress -> succeeded/failed). Static images finalize synchronously
    and carry no processing_info at all, so this is a no-op for them.
    """
    while processing_info and processing_info.get("state") in ("pending", "in_progress"):
        time.sleep(processing_info.get("check_after_secs", 1))
        processing_info = _media_status(access_token, media_id)

    if processing_info and processing_info.get("state") == "failed":
        error = processing_info.get("error") or {}
        reason = error.get("message") or error.get("name") or "unknown reason"
        raise PermanentError(f"X rejected media during processing: {reason}")


def _post_tweet(client: tweepy.Client, text: str, media_ids=None, in_reply_to_tweet_id=None) -> str:
    # tweepy.Client's write methods default to user_auth=True (OAuth 1.0a,
    # built from consumer_key/secret + access_token/secret) regardless of
    # whether a bearer_token was given to the Client — user_auth=False is
    # what actually routes the request through the OAuth 2.0 Bearer token
    # this module builds the Client with (_build_client).
    kwargs = {"text": text, "user_auth": False}
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
    if account_credentials is not None:
        source = "the job's Account credentials"
        values = {field: str(account_credentials.get(field, "")).strip() for field in _CREDENTIAL_FIELDS}
    else:
        source = "env vars (this job has no account_id)"
        values = {field: os.getenv(_ENV_VAR_BY_FIELD[field], "").strip() for field in _CREDENTIAL_FIELDS}

    missing = [field for field in _CREDENTIAL_FIELDS if not values[field]]
    if missing:
        raise PermanentError(f"X (Twitter) OAuth 2.0 credentials are missing from {source}: {', '.join(missing)}.")

    return values


def _build_client(creds: dict) -> tweepy.Client:
    # OAuth 2.0 user context: a single Bearer token, no per-request signing.
    return tweepy.Client(bearer_token=creds["access_token"])


def _classify_response_error(status: int | None, headers, text: str, context: str) -> PublishError | None:
    """
    Shared classification for both tweepy.HTTPException (tweet creation) and
    raw requests.Response (media upload) errors — both ultimately come down
    to an HTTP status code (+ headers), so both funnel through here. Returns
    None for a successful status (2xx), never raises itself.
    """
    if status is None:
        return None
    if 200 <= status < 300:
        return None

    if status == 429 or status >= 500:
        return TransientError(f"X API transient error {context} (HTTP {status}): {text}")

    if status == 401:
        # RFC 6750: a Bearer-auth resource server rejecting an expired or
        # otherwise invalid token reports it via WWW-Authenticate, e.g.
        # `Bearer error="invalid_token", error_description="..."`. This is
        # the distinguishing signal between "token needs a refresh" (worth
        # retrying after that) and "credentials are just wrong" (permanent)
        # — not yet verified against a live X response, see module docstring.
        www_authenticate = (headers or {}).get("WWW-Authenticate", "")
        if "invalid_token" in www_authenticate:
            return TokenExpiredError(f"X API access token is expired or invalid {context} (HTTP 401): {text}")
        return PermanentError(f"X API rejected the credentials {context} (HTTP 401): {text}")

    return PermanentError(f"X API rejected the request {context} (HTTP {status}): {text}")


def _classify_http_error(exc: tweepy.HTTPException) -> PublishError:
    response = exc.response
    status = response.status_code if response is not None else None
    headers = response.headers if response is not None else {}
    text = response.text if response is not None else str(exc)
    return _classify_response_error(status, headers, text, "posting to X") or PermanentError(
        f"X API rejected the request (HTTP {status}): {exc}"
    )


def _raise_for_media_error(response: requests.Response, context: str) -> dict:
    error = _classify_response_error(response.status_code, response.headers, response.text, context)
    if error is not None:
        raise error
    try:
        return response.json()
    except ValueError:
        # APPEND normally answers with an empty 2xx body; nothing to parse.
        return {}


def _raise_for_token_error(response: requests.Response, context: str) -> dict:
    """
    Classifies a response from X's OAuth 2.0 token endpoint. Same shape as
    TikTok's token endpoint (top-level "error"/"error_description" strings,
    RFC 6749 style) — handled with the same pattern as
    app/publishers/tiktok.py::_raise_for_token_error.
    """
    status = response.status_code
    try:
        body = response.json()
    except ValueError as exc:
        raise TransientError(f"X token endpoint returned a non-JSON response {context}: {exc}") from exc

    error = body.get("error")
    if not error:
        return body

    description = body.get("error_description", "")
    if status == 429 or status >= 500 or error in _TRANSIENT_TOKEN_ERROR_CODES:
        raise TransientError(f"X token endpoint transient error {context} ({error}): {description}")
    raise PermanentError(f"X token endpoint rejected the request {context} ({error}): {description}")


def _compute_expiry(expires_in) -> str | None:
    if not expires_in:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


def token_expires_within(credentials: dict, seconds: int) -> bool:
    """
    True if `credentials` (an Account.credentials dict) has no usable
    "expires_at", or one that falls within `seconds` from now. Used by
    app/tasks.py::refresh_expiring_tokens (Phase 8 pattern) to decide which
    Twitter accounts need a proactive refresh. Treating a missing/
    unparseable expiry as "needs refresh" is deliberate: a token we can't
    validate is safer to refresh than to silently trust.
    """
    expiry_raw = credentials.get("expires_at")
    if not expiry_raw:
        return True
    try:
        expiry = datetime.fromisoformat(expiry_raw)
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= datetime.now(timezone.utc) + timedelta(seconds=seconds)


def refresh_stored_credentials(credentials: dict) -> dict:
    """
    Force-refreshes a stored (Account-row) OAuth 2.0 credentials dict via
    POST https://api.twitter.com/2/oauth2/token (grant_type=refresh_token),
    authenticating as the confidential client with HTTP Basic auth
    (client_id:client_secret), per X's OAuth 2.0 docs.

    X's refresh tokens are SINGLE-USE and ROTATE: this call's response
    contains a NEW access_token AND a NEW refresh_token, and the old
    refresh_token is invalidated the moment this call succeeds. The caller
    (app/tasks.py) MUST persist the full returned dict onto the Account row
    before using the new access_token for anything else — losing the
    rotated refresh_token here strands the account just as surely as never
    refreshing at all.

    Same contract as youtube.py/tiktok.py's refresh_stored_credentials:
    returns the new credentials dict, or raises PermanentError if the
    refresh_token is invalid/revoked/already used (caller should deactivate
    the account and alert a human) or TransientError for anything else
    (network blip, a transient error from X's token endpoint) — the caller
    should just retry later.
    """
    client_id = str(credentials.get("client_id", "")).strip()
    client_secret = str(credentials.get("client_secret", "")).strip()
    refresh_token = str(credentials.get("refresh_token", "")).strip()
    missing = [
        name
        for name, value in (("client_id", client_id), ("client_secret", client_secret), ("refresh_token", refresh_token))
        if not value
    ]
    if missing:
        raise PermanentError(f"Stored X credentials are missing: {', '.join(missing)}.")

    response = requests.post(
        _TOKEN_URL,
        auth=(client_id, client_secret),
        headers=_FORM_HEADERS,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    body = _raise_for_token_error(response, "refreshing token")

    new_refresh_token = body.get("refresh_token")
    if not new_refresh_token:
        # X's refresh tokens always rotate; a response without one would
        # silently strand the account on the next refresh. Treat this as a
        # transient/unexpected server response rather than reusing the
        # now-invalidated old refresh_token as if nothing happened.
        raise TransientError(f"X token endpoint did not return a rotated refresh_token: {body}")

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": body["access_token"],
        "refresh_token": new_refresh_token,
        "expires_at": _compute_expiry(body.get("expires_in")),
    }
