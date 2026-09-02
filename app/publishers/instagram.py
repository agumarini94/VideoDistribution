"""
Instagram publisher (Phase 25) — the container/publish flow built on top of
Phase 23's Meta OAuth foundation (app/publishers/meta.py), same posture as
app/publishers/facebook.py: no real Meta App exists yet, so every code path
is exercised only against fully mocked HTTP (tests/test_publisher_instagram.py),
not a live account. Same contract as every other publisher in this package:
publish() is a pure function, no Celery/DB imports, and either returns a
result dict or raises a typed TransientError / PermanentError (or
TokenExpiredError, a TransientError subclass — see below).

Credentials: account_credentials is the job's Account.credentials dict, same
shape scripts/authorize_meta.py creates for platform "instagram" — ig_user_id
and page_token are all publish() needs (page_token is the access token used
for every call here; user_token is only used by meta.py's OAuth refresh
flow). Per Phase 23's decision, Meta has no env-var single-account fallback:
an Account row (account_id) is always required.

CRITICAL, unlike every other publisher in this package: Meta DOWNLOADS the
media from a public URL at publish time ("media must be hosted on a
publicly accessible server", per developers.facebook.com) — there is no
local-file upload path here at all. The job payload must already carry a
publicly-fetchable URL under "media_public_url" (the same field name
app/storage.py::upload_file's Phase 22 R2 staging attaches for
youtube/tiktok/facebook's single-media-file case — see dashboard/api.py and
scripts/enqueue_instagram_test.py, which upload to R2 as part of enqueueing
a job). Instagram has no text-only post type, so unlike facebook.py, media
is REQUIRED — "text" alone is not a valid payload.

Payload contract:
  {"text": optional str (caption), "media_public_url": str (required)}
Image vs. Reels video is auto-detected from media_public_url's guessed MIME
type (mimetypes.guess_type, same spirit as facebook.py's _media_kind) — no
separate "kind" flag needed. Since July 2023 all single feed videos publish
as Reels (per the phase brief), so the video branch always sends
media_type=REELS.

Endpoint shapes below are exactly what developers.facebook.com documents
(v26.0, graph.facebook.com — see app/publishers/meta.py's GRAPH_API_BASE),
supplied directly per the phase brief, not guessed:
  1. _create_container(): POST /<IG_USER_ID>/media
     image_url=<url>&caption=<text> (image), or
     media_type=REELS&video_url=<url>&caption=<text> (video)
     -> {"id": "<container_id>"}.
  2. _wait_for_container(): GET /<CONTAINER_ID>?fields=status_code, polled
     every _POLL_INTERVAL_SECONDS up to _POLL_TIMEOUT_SECONDS total.
     status_code: IN_PROGRESS -> keep polling; FINISHED -> proceed to step
     3; ERROR/EXPIRED -> PermanentError (recreating the container from
     scratch is the only recovery, which is exactly what happens if this
     job gets retried, since no container_id is persisted between
     attempts). Publishing before FINISHED returns a Graph API 400, so this
     step must complete before step 3 runs. A poll timeout is a
     TransientError, not PermanentError: the container may just need more
     time, and Celery's retry can pick the job up again later.
  3. _publish_container(): POST /<IG_USER_ID>/media_publish,
     creation_id=<container_id> -> {"id": "<ig_media_id>"}. The post is only
     actually live after this step.

Error classification, extended from meta.py exactly like facebook.py does:
every publish-time Graph call here passes
token_invalid_error_class=TokenExpiredError, so a code=190 (OAuthException)
error becomes TokenExpiredError instead of meta.py's default
PermanentError — app/tasks.py::publish_job's existing TokenExpiredError ->
refresh -> retry-once path (_handle_token_expired, Phase 21) already works
for this unchanged, since app/tasks.py's
_TOKEN_REFRESH_MODULES_BY_PLATFORM["instagram"] already pointed at meta.py
(Phase 23, originally only for the proactive Beat refresh — this phase is
what makes the reactive path actually reachable, by giving "instagram" a
real publish() that can raise TokenExpiredError).

NOT implemented here: the informational rate-limit endpoint (GET
/<IG_USER_ID>/content_publishing_limit, ~100 API-published posts/24h per
account, per the phase brief) — nothing in this project currently needs to
read it proactively; a caller that exceeds it will simply get a Graph API
error back from _create_container, classified like any other Graph error.

NOT implemented here: ffprobe-based pre-flight validation of Reels specs
(MP4/MOV, aspect ratio 0.01:1-10:1, 9:16 recommended, à la
app/publishers/youtube.py's Shorts validation, Phase 15). app/media_probe.py
operates on a local file path via ffprobe as a subprocess, but this
publisher only ever receives a public URL, never a local path — probing
would require first downloading the file back from R2, an extra network
round trip and more moving parts than "quick" for this phase. Documented as
a follow-up in CLAUDE.md rather than built now; Meta's own container
processing already rejects a malformed video via status_code=ERROR, so this
is a UX/fail-fast improvement, not a correctness gap.
"""

import mimetypes
import time

import requests

from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.publishers.meta import GRAPH_API_BASE, raise_for_graph_error

_CREDENTIAL_FIELDS = ("ig_user_id", "page_token")

# How often to poll the container's processing status, and the overall
# ceiling before giving up (see _wait_for_container below).
_POLL_INTERVAL_SECONDS = 5
_POLL_TIMEOUT_SECONDS = 300

_TERMINAL_ERROR_STATUSES = {"ERROR", "EXPIRED"}


def publish(platform: str, payload: dict, account_credentials: dict | None = None) -> dict:
    """
    Publishes an image or Reels video to Instagram: creates a media
    container from a public media URL, polls it until processing finishes,
    then publishes it. See the module docstring for the full payload
    contract and endpoint shapes.
    """
    try:
        credentials = _resolve_credentials(account_credentials)
        media_url = _validate_and_get_media_url(payload)
        kind = _media_kind(media_url)

        container_id = _create_container(credentials, payload, media_url, kind)
        _wait_for_container(credentials, container_id)
        media_id = _publish_container(credentials, container_id)

        return {"platform": "instagram", "external_id": media_id}
    except (TransientError, PermanentError):
        raise
    except requests.RequestException as exc:
        raise TransientError(f"Network error talking to the Instagram Graph API: {exc}") from exc
    except Exception as exc:  # normalize anything unexpected per the publisher contract
        raise TransientError(f"Unexpected error talking to the Instagram Graph API: {exc}") from exc


def _resolve_credentials(account_credentials: dict | None) -> dict:
    # Per Phase 23: Meta always uses Account rows, no env-var fallback like
    # youtube.py/twitter.py have — there's nowhere sensible for a
    # single-account IG token to live outside an Account row.
    if account_credentials is None:
        raise PermanentError(
            "Instagram publishing requires an Account (account_id) — there is no single-account/env-var "
            "fallback for this platform. Create one with scripts/authorize_meta.py."
        )
    values = {field: str(account_credentials.get(field, "")).strip() for field in _CREDENTIAL_FIELDS}
    missing = [field for field in _CREDENTIAL_FIELDS if not values[field]]
    if missing:
        raise PermanentError(f"Instagram Account credentials are missing: {', '.join(missing)}.")
    return values


def _validate_and_get_media_url(payload: dict) -> str:
    media_url = payload.get("media_public_url")
    if not media_url:
        raise PermanentError(
            "Instagram posts require media (payload['media_public_url']) — Instagram has no text-only post "
            "type. Meta downloads the media from a public URL at publish time, so it must already be staged "
            "to Cloudflare R2 (R2_ENDPOINT_URL/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET_NAME/"
            "R2_PUBLIC_BASE_URL must all be set — see app/storage.py and .env.example) before this job is "
            "created; a local-only file can't be used."
        )
    return media_url


def _media_kind(media_url: str) -> str:
    mime, _ = mimetypes.guess_type(media_url)
    if mime is not None and mime.startswith("image/"):
        return "image"
    if mime is not None and mime.startswith("video/"):
        return "video"
    raise PermanentError(f"Unsupported or undetectable media type for {media_url} (guessed mime type: {mime})")


def _create_container(credentials: dict, payload: dict, media_url: str, kind: str) -> str:
    data = {"access_token": credentials["page_token"]}
    if payload.get("text"):
        data["caption"] = payload["text"]
    if kind == "video":
        # Since July 2023, all single feed videos publish as Reels.
        data["media_type"] = "REELS"
        data["video_url"] = media_url
    else:
        data["image_url"] = media_url

    response = requests.post(f"{GRAPH_API_BASE}/{credentials['ig_user_id']}/media", data=data, timeout=30)
    body = raise_for_graph_error(response, "creating the media container", token_invalid_error_class=TokenExpiredError)
    container_id = body.get("id")
    if not container_id:
        raise PermanentError(f"Instagram media-container response is missing id: {body}")
    return container_id


def _get_container_status(credentials: dict, container_id: str) -> str:
    response = requests.get(
        f"{GRAPH_API_BASE}/{container_id}",
        params={"fields": "status_code", "access_token": credentials["page_token"]},
        timeout=30,
    )
    body = raise_for_graph_error(response, "checking media container status", token_invalid_error_class=TokenExpiredError)
    status_code = body.get("status_code")
    if not status_code:
        raise PermanentError(f"Instagram container-status response is missing status_code: {body}")
    return status_code


def _wait_for_container(credentials: dict, container_id: str) -> None:
    """
    Polls until the container reaches FINISHED (ready to publish),
    ERROR/EXPIRED (PermanentError — recreating the container from scratch
    is the only recovery), or _POLL_TIMEOUT_SECONDS elapses (TransientError
    — the container may just need more time; a Celery retry creates a fresh
    container on its next attempt, since no container_id is persisted
    between attempts).
    """
    elapsed = 0
    while True:
        status = _get_container_status(credentials, container_id)
        if status == "FINISHED":
            return
        if status in _TERMINAL_ERROR_STATUSES:
            raise PermanentError(f"Instagram media container {container_id} failed processing (status_code={status})")
        if elapsed >= _POLL_TIMEOUT_SECONDS:
            raise TransientError(
                f"Timed out after {_POLL_TIMEOUT_SECONDS}s waiting for Instagram media container "
                f"{container_id} to finish processing (last status_code={status})"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS


def _publish_container(credentials: dict, container_id: str) -> str:
    response = requests.post(
        f"{GRAPH_API_BASE}/{credentials['ig_user_id']}/media_publish",
        data={"creation_id": container_id, "access_token": credentials["page_token"]},
        timeout=30,
    )
    body = raise_for_graph_error(response, "publishing the media container", token_invalid_error_class=TokenExpiredError)
    media_id = body.get("id")
    if not media_id:
        raise PermanentError(f"Instagram media-publish response is missing id: {body}")
    return media_id
