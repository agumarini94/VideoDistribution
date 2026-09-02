"""
Facebook Pages publisher (Phase 24) — the first real publish() built on top
of Phase 23's Meta OAuth foundation (app/publishers/meta.py). Same contract
as every other publisher in this package: publish() is a pure function, no
Celery/DB imports, and either returns a result dict or raises a typed
TransientError / PermanentError (or TokenExpiredError, a TransientError
subclass — see below).

Built "ready waiting for credentials" like Phases 21/23: no real Meta App
exists yet, so every code path is exercised only against fully mocked HTTP
(tests/test_publisher_facebook.py), not a live account.

Credentials: account_credentials is the job's Account.credentials dict, same
shape scripts/authorize_meta.py creates for platform "facebook" — page_id
and page_token are all publish() needs (user_token is only used by the OAuth
refresh flow in meta.py). Per Phase 23's decision, Meta has no env-var
single-account fallback: an Account row (account_id) is always required.

Payload contract, one of three shapes:
  - text-only: {"text": str} -> POST /<page_id>/feed.
  - photo: {"text": optional str (caption), "media_paths": [one image path]}
    -> POST /<page_id>/photos (multipart "source").
  - video: {"text": optional str (description), "title": optional str,
    "media_paths": [one video path]} -> the 3-step Resumable Upload API
    (start session, upload binary, publish).
  Which of photo/video applies is auto-detected from the single
  media_paths file's guessed MIME type — no separate "kind" flag needed,
  same spirit as app/publishers/twitter.py's _media_kind.

Endpoint shapes below are exactly what developers.facebook.com documents,
supplied directly (not guessed) — graph-video.facebook.com is deprecated,
everything goes through graph.facebook.com (see app/publishers/meta.py's
GRAPH_API_BASE, v26.0). Error classification is meta.py's shared
raise_for_graph_error, extended here: a code=190 (OAuthException) error from
any of this module's publish-time Graph calls (feed/photos/videos, the
upload-session start, the upload binary POST, and the offset-check GET)
raises TokenExpiredError instead of meta.py's default PermanentError, so
app/tasks.py::publish_job's existing TokenExpiredError -> refresh ->
retry-once path (_handle_token_expired, Phase 21) kicks in — it's already
generic over any platform whose _TOKEN_REFRESH_MODULES_BY_PLATFORM entry
exposes refresh_stored_credentials, which meta.py already does for
"facebook" (Phase 23), so no changes were needed there.

Resumable video upload (3 steps):
  1. _start_upload_session(): POST /<APP_ID>/uploads?file_name&file_length&
     file_type&access_token=<PAGE_TOKEN> -> {"id": "upload:<SESSION_ID>"}.
     APP_ID comes from env META_APP_ID (app-level, not per-account) — the
     only place this module reads an env var directly, mirroring how
     meta.py's _app_credentials() is the only place that reads
     META_APP_ID/META_APP_SECRET.
  2. _upload_video_binary(): POST /upload:<SESSION_ID>, header
     `Authorization: OAuth <PAGE_TOKEN>` + `file_offset: <n>`, raw binary
     body -> {"h": "<FILE_HANDLE>"}. If this fails partway (network error,
     or a transient Graph error), _get_upload_offset() (GET
     /upload:<SESSION_ID>, same OAuth header) reports how many bytes the
     server actually received, and the POST is retried from that byte
     offset instead of restarting the whole file — up to
     _MAX_UPLOAD_ATTEMPTS attempts total.
  3. _publish_video(): POST /<page_id>/videos,
     fbuploader_video_file_chunk=<FILE_HANDLE> + title/description +
     access_token=<PAGE_TOKEN> -> {"id": "<video_id>"}. The video is only
     actually live after this step — steps 1-2 alone don't publish
     anything.
"""

import mimetypes
import os
from pathlib import Path

import requests

from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.publishers.meta import GRAPH_API_BASE, raise_for_graph_error

_CREDENTIAL_FIELDS = ("page_id", "page_token")

# A single resumable-upload POST carries the whole file; on failure we ask
# the server how many bytes it actually got (_get_upload_offset) and retry
# from there instead of restarting from byte 0. Bounded so a persistently
# broken upload session eventually surfaces as a TransientError rather than
# looping forever.
_MAX_UPLOAD_ATTEMPTS = 3


def publish(platform: str, payload: dict, account_credentials: dict | None = None) -> dict:
    """
    Posts to a Facebook Page: text (payload["text"]), a photo, or a video —
    see the module docstring for the full payload contract and how photo vs.
    video is chosen.
    """
    try:
        _validate_top_level_payload(payload)
        credentials = _resolve_credentials(account_credentials)

        media_paths = payload.get("media_paths")
        if media_paths:
            path = _validate_single_media_path(media_paths)
            kind = _media_kind(path)
            if kind == "image":
                return _publish_photo(payload, credentials, path)
            return _publish_video(payload, credentials, path)
        return _publish_text(payload, credentials)
    except (TransientError, PermanentError):
        raise
    except requests.RequestException as exc:
        raise TransientError(f"Network error talking to the Facebook Graph API: {exc}") from exc
    except Exception as exc:  # normalize anything unexpected per the publisher contract
        raise TransientError(f"Unexpected error talking to the Facebook Graph API: {exc}") from exc


def _validate_top_level_payload(payload: dict) -> None:
    has_text = bool(payload.get("text"))
    has_media = bool(payload.get("media_paths"))
    if not has_text and not has_media:
        raise PermanentError("Payload must include 'text' and/or 'media_paths'")


def _resolve_credentials(account_credentials: dict | None) -> dict:
    # Per Phase 23: Meta always uses Account rows, no env-var fallback like
    # youtube.py/twitter.py have — there's nowhere sensible for a
    # single-account Page token to live outside an Account row.
    if account_credentials is None:
        raise PermanentError(
            "Facebook publishing requires an Account (account_id) — there is no single-account/env-var "
            "fallback for this platform. Create one with scripts/authorize_meta.py."
        )
    values = {field: str(account_credentials.get(field, "")).strip() for field in _CREDENTIAL_FIELDS}
    missing = [field for field in _CREDENTIAL_FIELDS if not values[field]]
    if missing:
        raise PermanentError(f"Facebook Account credentials are missing: {', '.join(missing)}.")
    return values


def _media_kind(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime is not None and mime.startswith("image/"):
        return "image"
    if mime is not None and mime.startswith("video/"):
        return "video"
    raise PermanentError(f"Unsupported or undetectable media type for {path} (guessed mime type: {mime})")


def _validate_single_media_path(media_paths) -> Path:
    if not isinstance(media_paths, list) or len(media_paths) != 1:
        raise PermanentError(f"Facebook posts support exactly one media_paths entry, got {media_paths!r}")
    path = Path(media_paths[0])
    if not path.is_file():
        raise PermanentError(f"Media file not found: {path}")
    return path


def _app_id() -> str:
    app_id = os.getenv("META_APP_ID", "").strip()
    if not app_id:
        raise PermanentError(
            "META_APP_ID is not configured (needed to start a Facebook resumable video upload session). "
            "Set it in .env."
        )
    return app_id


def _publish_text(payload: dict, credentials: dict) -> dict:
    text = payload.get("text")
    if not text:
        raise PermanentError("Missing required payload field: text")

    response = requests.post(
        f"{GRAPH_API_BASE}/{credentials['page_id']}/feed",
        data={"message": text, "access_token": credentials["page_token"]},
        timeout=30,
    )
    body = raise_for_graph_error(response, "posting to the Page feed", token_invalid_error_class=TokenExpiredError)
    post_id = body.get("id")
    if not post_id:
        raise PermanentError(f"Facebook feed post response is missing id: {body}")
    return {"platform": "facebook", "external_id": post_id}


def _publish_photo(payload: dict, credentials: dict, path: Path) -> dict:
    mime, _ = mimetypes.guess_type(str(path))
    data = {"access_token": credentials["page_token"]}
    if payload.get("text"):
        data["caption"] = payload["text"]

    with path.open("rb") as f:
        files = {"source": (path.name, f, mime or "application/octet-stream")}
        response = requests.post(f"{GRAPH_API_BASE}/{credentials['page_id']}/photos", data=data, files=files, timeout=120)

    body = raise_for_graph_error(response, "posting a photo", token_invalid_error_class=TokenExpiredError)
    post_id = body.get("post_id") or body.get("id")
    if not post_id:
        raise PermanentError(f"Facebook photo post response is missing id/post_id: {body}")
    return {"platform": "facebook", "external_id": post_id}


def _publish_video(payload: dict, credentials: dict, path: Path) -> dict:
    app_id = _app_id()
    page_token = credentials["page_token"]
    mime, _ = mimetypes.guess_type(str(path))
    file_length = path.stat().st_size

    session_id = _start_upload_session(app_id, page_token, path.name, file_length, mime)
    file_handle = _upload_video_binary(page_token, session_id, path)

    data = {"fbuploader_video_file_chunk": file_handle, "access_token": page_token}
    if payload.get("text"):
        data["description"] = payload["text"]
    if payload.get("title"):
        data["title"] = payload["title"]

    response = requests.post(f"{GRAPH_API_BASE}/{credentials['page_id']}/videos", data=data, timeout=120)
    body = raise_for_graph_error(response, "publishing the uploaded video", token_invalid_error_class=TokenExpiredError)
    video_id = body.get("id")
    if not video_id:
        raise PermanentError(f"Facebook video publish response is missing id: {body}")
    return {"platform": "facebook", "external_id": video_id}


def _start_upload_session(app_id: str, page_token: str, file_name: str, file_length: int, mime: str | None) -> str:
    response = requests.post(
        f"{GRAPH_API_BASE}/{app_id}/uploads",
        params={
            "file_name": file_name,
            "file_length": file_length,
            "file_type": mime or "application/octet-stream",
            "access_token": page_token,
        },
        timeout=30,
    )
    body = raise_for_graph_error(
        response, "starting a resumable video upload session", token_invalid_error_class=TokenExpiredError
    )
    session_ref = body.get("id", "")
    if not session_ref.startswith("upload:"):
        raise PermanentError(f"Facebook upload-session response is missing a valid id: {body}")
    return session_ref[len("upload:") :]


def _upload_headers(page_token: str, offset: int) -> dict:
    return {"Authorization": f"OAuth {page_token}", "file_offset": str(offset)}


def _get_upload_offset(page_token: str, session_id: str) -> int:
    response = requests.get(
        f"{GRAPH_API_BASE}/upload:{session_id}",
        headers={"Authorization": f"OAuth {page_token}"},
        timeout=30,
    )
    body = raise_for_graph_error(response, "checking the resumable upload offset", token_invalid_error_class=TokenExpiredError)
    offset = body.get("file_offset")
    if offset is None:
        raise PermanentError(f"Facebook upload-offset response is missing file_offset: {body}")
    return int(offset)


def _upload_video_binary(page_token: str, session_id: str, path: Path) -> str:
    """
    Uploads the whole file in a single POST, per Meta's Resumable Upload
    API. If the POST is interrupted (a network error, or a transient Graph
    error) partway through, the amount the server actually received is
    fetched via _get_upload_offset() and the POST is retried starting from
    that byte, up to _MAX_UPLOAD_ATTEMPTS total attempts — never restarting
    from byte 0 after a partial upload.

    TokenExpiredError is deliberately NOT retried here (it's a TransientError
    subclass, so it would otherwise be caught by the same except clause as an
    ordinary network blip): it must propagate all the way to
    app/tasks.py::publish_job's reactive refresh-and-retry-once path
    unchanged, not get relabeled as a plain TransientError by this loop.
    """
    offset = 0
    last_error: Exception | None = None

    for attempt in range(1, _MAX_UPLOAD_ATTEMPTS + 1):
        try:
            with path.open("rb") as f:
                f.seek(offset)
                data = f.read()
            response = requests.post(
                f"{GRAPH_API_BASE}/upload:{session_id}",
                headers=_upload_headers(page_token, offset),
                data=data,
                timeout=300,
            )
            body = raise_for_graph_error(response, "uploading video binary", token_invalid_error_class=TokenExpiredError)
            file_handle = body.get("h")
            if not file_handle:
                raise PermanentError(f"Facebook upload response is missing the file handle (h): {body}")
            return file_handle
        except TokenExpiredError:
            raise
        except (requests.RequestException, TransientError) as exc:
            last_error = exc
            if attempt >= _MAX_UPLOAD_ATTEMPTS:
                break
            offset = _get_upload_offset(page_token, session_id)

    raise TransientError(
        f"Uploading video binary to Facebook failed after {_MAX_UPLOAD_ATTEMPTS} attempts: {last_error}"
    ) from last_error
