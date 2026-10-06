"""
Pinterest publisher (Phase 29e) — OAuth (no PKCE) + Pin creation, built
"ready waiting for credentials" like every other publisher in this package
before real credentials exist: no real Pinterest Developer app exists yet,
so every code path is exercised only against fully mocked HTTP
(tests/test_publisher_pinterest.py), not a live account. Same contract as
every other publisher: publish() is a pure function, no Celery/DB imports,
and either returns a result dict or raises a typed TransientError /
PermanentError (or TokenExpiredError, a TransientError subclass).

Endpoint shapes below come from developers.pinterest.com (fetched directly
this session, not guessed by analogy to another platform) — see CLAUDE.md
Phase 29e for the citations. A few specifics (the exact error response body
shape, and whether the resumable-video-upload POST to S3 ever returns a
Pinterest-style JSON error) could not be independently confirmed against
rendered docs, and are flagged inline rather than asserted as fact — same
posture as every other publisher's error-code table in this package before
real credentials exist.

OAuth (no PKCE — confirmed: Pinterest's docs don't mention a code_challenge
param, unlike X's/TikTok's flows):
  1. Browser dialog: GET https://www.pinterest.com/oauth/
       ?client_id&redirect_uri&response_type=code&scope&state
     -> redirects back with ?code=...&state=...
  2. Code -> access_token + refresh_token: exchange_code_for_credentials().
  3. Refresh (ROTATING, like Twitter's — NOT like Meta's): Pinterest's
     "continuous refresh" tokens return a brand new access_token AND a new
     refresh_token on every refresh, invalidating the one just used.
     refresh_stored_credentials() below carries the same "missing rotated
     refresh_token -> TransientError, never reuse the stale one" safeguard
     as app/publishers/twitter.py::refresh_stored_credentials.

Design choice worth flagging explicitly: unlike twitter.py (which stores
client_id/client_secret duplicated inside every Account row's credentials,
so refresh_stored_credentials needs nothing beyond its one dict argument),
this module follows app/publishers/meta.py's/facebook.py's pattern instead —
PINTEREST_APP_ID/PINTEREST_APP_SECRET are a single app-level secret, read
directly from the environment via _app_credentials() wherever needed
(build_authorization_url, exchange_code_for_credentials,
refresh_stored_credentials), and are never duplicated into each Account's
stored credentials. This avoids storing an app secret once per connected
account for no benefit; Pinterest's Account.credentials only ever needs
access_token/refresh_token/expires_at.

Credentials: account_credentials is the job's Account.credentials dict —
{access_token, refresh_token, expires_at}. No env-var single-account
fallback (same posture as tiktok.py/facebook.py/instagram.py, not
twitter.py/youtube.py's older env-fallback pattern) — every platform="pinterest"
job needs an Account row, created via the dashboard's self-service "Connect
Pinterest" OAuth flow (dashboard/api.py) or scripts/add_account.py.

Payload contract — board_id and title are both required (Pinterest's own
Pin-creation API requires both); exactly one of these two media sources,
never both:
  - "media_public_url": an image (PNG/JPEG/etc, auto-detected by guessed
    MIME type) — the same field name app/storage.py::upload_file's R2
    staging attaches (see dashboard/api.py). Pinterest downloads the image
    straight from this URL via media_source={"source_type": "image_url"} —
    a single POST /v5/pins call, no separate upload step.
  - "media_paths": a single local video file path. Pinterest video Pins
    CANNOT use a direct video_url the way an image can — per
    developers.pinterest.com, uploading the raw file through Pinterest's
    own 3-step flow is mandatory:
      1. POST /v5/media {"media_type": "video"} -> {media_id, upload_url,
         upload_parameters} (an AWS S3 presigned-POST policy).
      2. POST <upload_url> (multipart: upload_parameters fields + the file)
         — NOT a Pinterest API call, so Pinterest's own error-body shape
         doesn't apply; a non-2xx here is treated as TransientError
         (unverified — could also be a permanently-malformed request, but
         a presigned-POST failure is far more often transient/expired-policy
         than a content problem).
      3. GET /v5/media/{media_id} polled until status="succeeded" (or
         "failed" -> PermanentError; timeout -> TransientError, same
         pattern as app/publishers/instagram.py::_wait_for_container).
      4. POST /v5/pins with media_source={"source_type": "video_id",
         "media_id": ..., plus either "cover_image_url" (if
         payload["cover_image_url"] was given) or
         "cover_image_key_frame_time": 0 (default — first frame of the
         video) otherwise, per the phase brief's explicit instruction.
  Optional: "description" (falls back to payload["text"] if "description"
  isn't given, so the dashboard's generic composer "text" field doubles as
  the Pin description, same reuse as every other platform's payload
  builder in dashboard/api.py), "link", "cover_image_url" (video only).

  Result dict: {"platform": "pinterest", "external_id": <pin_id>,
  "board_id": <board_id>}.

list_boards(credentials) — GET /v5/boards, paginated via Pinterest's
"bookmark" cursor convention — is used only by dashboard/api.py's
GET /api/accounts/{id}/boards (the Composer's board picker), never by
publish() or app/tasks.py. A 401 there raises the default PermanentError,
not TokenExpiredError: listing boards isn't part of the publish/retry
pipeline, so there's no reactive-refresh path to feed (same reasoning as
app/publishers/meta.py::list_pages).

Error classification (_raise_for_api_error): the exact Pinterest error body
shape could not be confirmed against rendered docs this session, so
classification leans on HTTP status alone, same defensive posture as
app/publishers/twitter.py's _classify_response_error: 429/5xx ->
TransientError; 401 -> token_invalid_error_class (PermanentError by
default; publish()'s own Graph-style calls pass TokenExpiredError so
app/tasks.py::publish_job's existing refresh-and-retry-once path, Phase 21,
works unchanged); any other 4xx -> PermanentError. If the response body
happens to carry a "code"/"message" field (as Pinterest's public error
examples suggest), it's folded into the exception message for
diagnostics, but never used to decide Transient vs. Permanent.
"""

import mimetypes
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

from app.exceptions import PermanentError, TokenExpiredError, TransientError

AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
_API_BASE = "https://api.pinterest.com/v5"
_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}

# Confirmed per CLAUDE.md Phase 29e point 1: minimal scopes only — reading
# (public) boards and creating (public) pins. Not boards:read_secret or
# pins:read — neither is needed by anything this module does.
_OAUTH_SCOPES = "boards:read,pins:write"

# How often / how long to poll GET /v5/media/{id} while Pinterest processes
# an uploaded video, before giving up — same constants/semantics as
# app/publishers/instagram.py's container-processing poll.
_MEDIA_POLL_INTERVAL_SECONDS = 5
_MEDIA_POLL_TIMEOUT_SECONDS = 300

_CREDENTIAL_FIELDS = ("access_token",)


def _app_credentials() -> tuple[str, str]:
    app_id = os.getenv("PINTEREST_APP_ID", "").strip()
    app_secret = os.getenv("PINTEREST_APP_SECRET", "").strip()
    missing = [name for name, value in (("PINTEREST_APP_ID", app_id), ("PINTEREST_APP_SECRET", app_secret)) if not value]
    if missing:
        raise PermanentError(f"Pinterest app credentials are not configured (missing: {', '.join(missing)}). Set them in .env.")
    return app_id, app_secret


def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _compute_expiry(expires_in) -> str | None:
    if not expires_in:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


def publish(platform: str, payload: dict, account_credentials: dict | None = None) -> dict:
    """
    Creates a Pinterest Pin: an image (payload["media_public_url"]) or a
    video (payload["media_paths"], a single local file) on the given board
    (payload["board_id"]) — see the module docstring for the full payload
    contract.
    """
    try:
        credentials = _resolve_credentials(account_credentials)
        board_id, title, description, link, cover_image_url = _validate_top_level_payload(payload)

        media_public_url = payload.get("media_public_url")
        if media_public_url:
            kind = _media_kind(media_public_url)
            if kind != "image":
                raise PermanentError(
                    "payload['media_public_url'] must be an image — Pinterest video Pins require the local-file "
                    "upload flow (payload['media_paths']), not a public URL."
                )
            pin_id = _create_image_pin(credentials, board_id, title, description, link, media_public_url)
        else:
            path = _validate_single_video_path(payload["media_paths"])
            pin_id = _create_video_pin(credentials, board_id, title, description, link, path, cover_image_url)

        return {"platform": "pinterest", "external_id": pin_id, "board_id": board_id}
    except (TransientError, PermanentError):
        raise
    except requests.RequestException as exc:
        raise TransientError(f"Network error talking to the Pinterest API: {exc}") from exc
    except Exception as exc:  # normalize anything unexpected per the publisher contract
        raise TransientError(f"Unexpected error talking to the Pinterest API: {exc}") from exc


def _validate_top_level_payload(payload: dict) -> tuple[str, str, str | None, str | None, str | None]:
    board_id = payload.get("board_id")
    if not board_id:
        raise PermanentError("Payload must include 'board_id' (the destination Pinterest board)")

    title = payload.get("title")
    if not title:
        raise PermanentError("Payload must include 'title' (required by Pinterest's Pin-creation API)")

    has_public_url = bool(payload.get("media_public_url"))
    has_local_path = bool(payload.get("media_paths"))
    if has_public_url and has_local_path:
        raise PermanentError(
            "Payload cannot include both 'media_public_url' and 'media_paths' — provide exactly one media source"
        )
    if not has_public_url and not has_local_path:
        raise PermanentError(
            "Payload must include media: 'media_public_url' (an image, staged via R2) or 'media_paths' "
            "(a single local video file, uploaded through Pinterest's own upload flow)"
        )

    description = payload.get("description") or payload.get("text")
    return str(board_id), str(title), description, payload.get("link"), payload.get("cover_image_url")


def _resolve_credentials(account_credentials: dict | None) -> dict:
    # No env-var single-account fallback — same posture as tiktok.py/
    # facebook.py/instagram.py: there's nowhere sensible for a single
    # Pinterest access token to live outside an Account row.
    if account_credentials is None:
        raise PermanentError(
            "Pinterest publishing requires an Account (account_id) — there is no single-account/env-var "
            "fallback for this platform. Connect a Pinterest account via the dashboard's self-service "
            '"Connect Pinterest" button, or scripts/add_account.py.'
        )
    values = {field: str(account_credentials.get(field, "")).strip() for field in _CREDENTIAL_FIELDS}
    missing = [field for field in _CREDENTIAL_FIELDS if not values[field]]
    if missing:
        raise PermanentError(f"Pinterest Account credentials are missing: {', '.join(missing)}.")
    return values


def _media_kind(url_or_path: str) -> str:
    mime, _ = mimetypes.guess_type(url_or_path)
    if mime is not None and mime.startswith("image/"):
        return "image"
    if mime is not None and mime.startswith("video/"):
        return "video"
    raise PermanentError(f"Unsupported or undetectable media type for {url_or_path} (guessed mime type: {mime})")


def _validate_single_video_path(media_paths) -> Path:
    if not isinstance(media_paths, list) or len(media_paths) != 1:
        raise PermanentError(f"Pinterest video pins support exactly one media_paths entry, got {media_paths!r}")
    path = Path(media_paths[0])
    if not path.is_file():
        raise PermanentError(f"Media file not found: {path}")
    kind = _media_kind(str(path))
    if kind != "video":
        raise PermanentError(
            f"media_paths is only used for Pinterest video pins (got a {kind} at {path}) — use "
            "media_public_url for an image Pin."
        )
    return path


def _raise_for_api_error(
    response: requests.Response, context: str, token_invalid_error_class: type[Exception] = PermanentError
) -> dict:
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = {}

    if 200 <= status < 300:
        return body

    message = body.get("message") or response.text
    code = body.get("code")
    if status == 429 or status >= 500:
        raise TransientError(f"Pinterest API transient error {context} (HTTP {status}, code={code}): {message}")
    if status == 401:
        raise token_invalid_error_class(f"Pinterest API rejected the access token {context} (HTTP {status}): {message}")
    raise PermanentError(f"Pinterest API rejected the request {context} (HTTP {status}, code={code}): {message}")


def _create_image_pin(
    credentials: dict, board_id: str, title: str, description: str | None, link: str | None, image_url: str
) -> str:
    data = {"board_id": board_id, "title": title, "media_source": {"source_type": "image_url", "url": image_url}}
    if description:
        data["description"] = description
    if link:
        data["link"] = link

    response = requests.post(
        f"{_API_BASE}/pins",
        headers={**_auth_headers(credentials["access_token"]), "Content-Type": "application/json"},
        json=data,
        timeout=30,
    )
    body = _raise_for_api_error(response, "creating an image Pin", token_invalid_error_class=TokenExpiredError)
    pin_id = body.get("id")
    if not pin_id:
        raise PermanentError(f"Pinterest Pin-creation response is missing id: {body}")
    return str(pin_id)


def _register_media_upload(credentials: dict) -> tuple[str, str, dict]:
    response = requests.post(
        f"{_API_BASE}/media",
        headers={**_auth_headers(credentials["access_token"]), "Content-Type": "application/json"},
        json={"media_type": "video"},
        timeout=30,
    )
    body = _raise_for_api_error(response, "registering a video upload", token_invalid_error_class=TokenExpiredError)
    media_id = body.get("media_id")
    upload_url = body.get("upload_url")
    upload_parameters = body.get("upload_parameters")
    if not media_id or not upload_url or not isinstance(upload_parameters, dict):
        raise PermanentError(f"Pinterest media-registration response is missing required fields: {body}")
    return str(media_id), upload_url, upload_parameters


def _upload_video_binary(upload_url: str, upload_parameters: dict, path: Path) -> None:
    """
    Uploads the raw video file to the presigned S3 URL Pinterest's media
    registration step returned. This is NOT a Pinterest API call — it's a
    plain AWS S3 presigned-POST — so Pinterest's own JSON error-body shape
    doesn't apply here; a non-2xx response is treated as TransientError
    (unverified: could also signal a permanently-malformed/expired upload
    policy, but a presigned-POST failure is far more often transient).
    """
    fields = {key: (None, str(value)) for key, value in upload_parameters.items()}
    with path.open("rb") as f:
        files = {**fields, "file": (path.name, f, "application/octet-stream")}
        response = requests.post(upload_url, files=files, timeout=300)

    if not (200 <= response.status_code < 300):
        raise TransientError(f"Uploading video to Pinterest's media storage failed (HTTP {response.status_code}): {response.text}")


def _wait_for_media_processed(credentials: dict, media_id: str) -> None:
    """
    Polls GET /v5/media/{media_id} until status="succeeded" ("failed" ->
    PermanentError; anything else, including an unrecognized status, keeps
    polling until _MEDIA_POLL_TIMEOUT_SECONDS elapses -> TransientError —
    same pattern as app/publishers/instagram.py::_wait_for_container).
    """
    elapsed = 0
    status = None
    while True:
        response = requests.get(
            f"{_API_BASE}/media/{media_id}",
            headers=_auth_headers(credentials["access_token"]),
            timeout=30,
        )
        body = _raise_for_api_error(response, "checking video processing status", token_invalid_error_class=TokenExpiredError)
        status = body.get("status")
        if status == "succeeded":
            return
        if status == "failed":
            raise PermanentError(f"Pinterest failed to process the uploaded video (media_id={media_id}): {body}")
        if elapsed >= _MEDIA_POLL_TIMEOUT_SECONDS:
            raise TransientError(
                f"Timed out after {_MEDIA_POLL_TIMEOUT_SECONDS}s waiting for Pinterest to process video "
                f"media_id={media_id} (last status={status})"
            )
        time.sleep(_MEDIA_POLL_INTERVAL_SECONDS)
        elapsed += _MEDIA_POLL_INTERVAL_SECONDS


def _create_video_pin(
    credentials: dict,
    board_id: str,
    title: str,
    description: str | None,
    link: str | None,
    path: Path,
    cover_image_url: str | None,
) -> str:
    media_id, upload_url, upload_parameters = _register_media_upload(credentials)
    _upload_video_binary(upload_url, upload_parameters, path)
    _wait_for_media_processed(credentials, media_id)

    media_source = {"source_type": "video_id", "media_id": media_id}
    if cover_image_url:
        media_source["cover_image_url"] = cover_image_url
    else:
        # Phase 29e point 3: no cover_image_url given -> default to the
        # video's own first frame rather than requiring a separate cover
        # image upload.
        media_source["cover_image_key_frame_time"] = 0

    data = {"board_id": board_id, "title": title, "media_source": media_source}
    if description:
        data["description"] = description
    if link:
        data["link"] = link

    response = requests.post(
        f"{_API_BASE}/pins",
        headers={**_auth_headers(credentials["access_token"]), "Content-Type": "application/json"},
        json=data,
        timeout=30,
    )
    body = _raise_for_api_error(response, "creating a video Pin", token_invalid_error_class=TokenExpiredError)
    pin_id = body.get("id")
    if not pin_id:
        raise PermanentError(f"Pinterest Pin-creation response is missing id: {body}")
    return str(pin_id)


def list_boards(credentials: dict) -> list[dict]:
    """
    GET /v5/boards, paginated via Pinterest's "bookmark" cursor convention
    (a page's response carries the next page's bookmark, or omits/nulls it
    on the last page). Used only by dashboard/api.py's board-picker endpoint
    (GET /api/accounts/{id}/boards) — never by publish() or app/tasks.py, so
    a 401 here raises the default PermanentError, not TokenExpiredError.
    """
    values = _resolve_credentials(credentials)
    boards: list[dict] = []
    bookmark = None
    while True:
        params = {"page_size": 100}
        if bookmark:
            params["bookmark"] = bookmark
        response = requests.get(f"{_API_BASE}/boards", headers=_auth_headers(values["access_token"]), params=params, timeout=30)
        body = _raise_for_api_error(response, "listing boards")
        items = body.get("items") or []
        boards.extend({"id": str(item["id"]), "name": item.get("name") or str(item["id"])} for item in items if item.get("id"))
        bookmark = body.get("bookmark")
        if not bookmark:
            break
    return boards


def token_expires_within(credentials: dict, seconds: int) -> bool:
    """
    True if `credentials` (an Account.credentials dict) has no usable
    "expires_at", or one that falls within `seconds` from now. Same contract
    as every other publisher's token_expires_within in this package, used by
    app/tasks.py::refresh_expiring_tokens — registered with a 24-hour window
    (Phase 29e point 2) since Pinterest access tokens last 30 days, far
    longer than the 45-minute default but not wide enough to need Meta's
    7-day window.
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


def _raise_for_token_error(response: requests.Response, context: str) -> dict:
    """
    Classifies a response from Pinterest's OAuth token endpoint. Assumed to
    follow the same RFC 6749-style top-level "error"/"error_description"
    shape as X's/TikTok's token endpoints (app/publishers/twitter.py's/
    tiktok.py's _raise_for_token_error) — not independently confirmed against
    a real Pinterest response yet.
    """
    status = response.status_code
    try:
        body = response.json()
    except ValueError as exc:
        raise TransientError(f"Pinterest token endpoint returned a non-JSON response {context}: {exc}") from exc

    error = body.get("error")
    if not error:
        return body

    description = body.get("error_description") or body.get("message") or ""
    if status == 429 or status >= 500:
        raise TransientError(f"Pinterest token endpoint transient error {context} ({error}): {description}")
    raise PermanentError(f"Pinterest token endpoint rejected the request {context} ({error}): {description}")


def refresh_stored_credentials(credentials: dict) -> dict:
    """
    Force-refreshes a stored (Account-row) credentials dict via
    POST https://api.pinterest.com/v5/oauth/token (grant_type=refresh_token),
    authenticating as the confidential app with HTTP Basic auth
    (PINTEREST_APP_ID:PINTEREST_APP_SECRET, read via _app_credentials() —
    see the module docstring for why these aren't duplicated into the
    stored credentials the way twitter.py's client_id/client_secret are).

    Pinterest's "continuous refresh" tokens are SINGLE-USE and ROTATE, same
    as X's: this call's response contains a NEW access_token AND a NEW
    refresh_token, and the old refresh_token is invalidated the moment this
    call succeeds. The caller (app/tasks.py) MUST persist the full returned
    dict onto the Account row before using the new access_token for
    anything else — losing the rotated refresh_token here strands the
    account just as surely as never refreshing at all.

    Same contract as every other publisher's refresh_stored_credentials:
    returns the new credentials dict, or raises PermanentError if the
    refresh_token is invalid/revoked/already used (caller should deactivate
    the account and alert a human) or TransientError for anything else
    (network blip, a transient error from Pinterest's token endpoint, or a
    response missing the rotated refresh_token) — the caller should just
    retry later.
    """
    refresh_token = str(credentials.get("refresh_token", "")).strip()
    if not refresh_token:
        raise PermanentError("Stored Pinterest credentials are missing: refresh_token.")

    app_id, app_secret = _app_credentials()
    response = requests.post(
        _TOKEN_URL,
        auth=(app_id, app_secret),
        headers=_FORM_HEADERS,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    body = _raise_for_token_error(response, "refreshing token")

    new_refresh_token = body.get("refresh_token")
    if not new_refresh_token:
        # Pinterest's refresh tokens always rotate; a response without one
        # would silently strand the account on the next refresh. Treat this
        # as a transient/unexpected server response rather than reusing the
        # now-invalidated old refresh_token as if nothing happened.
        raise TransientError(f"Pinterest token endpoint did not return a rotated refresh_token: {body}")

    return {
        "access_token": body["access_token"],
        "refresh_token": new_refresh_token,
        "expires_at": _compute_expiry(body.get("expires_in")),
    }


def build_authorization_url(redirect_uri: str, state: str) -> str:
    """
    Builds Pinterest's OAuth consent-screen URL (the in-browser counterpart
    to a CLI authorize script — Pinterest has none, this is the only
    authorization flow this platform ever gets). No PKCE (confirmed:
    developers.pinterest.com's authorization-endpoint docs list only
    client_id/redirect_uri/response_type/scope/state — unlike X's/TikTok's
    flows, there is no code_challenge param), so the caller (dashboard/api.py)
    needs no verifier/challenge pair — state only carries client_id/user_id,
    same shape as YouTube's/Meta's.

    Reads PINTEREST_APP_ID from the environment (app-level, no Account row
    exists yet at this point in the flow — same reasoning as
    twitter.py/tiktok.py/meta.py reading their own app credentials
    directly); raises PermanentError if unset (or if PINTEREST_APP_SECRET is
    also unset, via _app_credentials() — the secret isn't used here, but
    requiring both up front matches every other credential check in this
    module and fails clearly before a redirect that would later dead-end at
    the token exchange anyway).
    """
    app_id, _app_secret = _app_credentials()
    query = urlencode(
        {"client_id": app_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": _OAUTH_SCOPES, "state": state}
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code_for_credentials(code: str, redirect_uri: str) -> dict:
    """
    Exchanges an authorization code (from the browser redirect back after
    build_authorization_url's consent screen) for an access_token +
    refresh_token pair, via the same token endpoint (_TOKEN_URL) and
    confidential-app HTTP Basic auth as refresh_stored_credentials above.

    Unlike refresh_stored_credentials (called from the proactive Beat task
    and the reactive retry-once path, where distinguishing transient vs.
    permanent matters so the caller can decide whether to retry
    automatically), this is a one-shot interactive flow driven by a human
    sitting at the consent screen — any failure just means clicking
    "Connect" again — so every failure here normalizes to PermanentError,
    same contract as app/publishers/youtube.py's/twitter.py's/tiktok.py's
    exchange_code_for_credentials.
    """
    app_id, app_secret = _app_credentials()
    try:
        response = requests.post(
            _TOKEN_URL,
            auth=(app_id, app_secret),
            headers=_FORM_HEADERS,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            timeout=30,
        )
        body = _raise_for_token_error(response, "exchanging authorization code")
    except (TransientError, PermanentError) as exc:
        raise PermanentError(f"Failed to exchange the Pinterest authorization code for a token: {exc}") from exc
    except requests.RequestException as exc:
        raise PermanentError(f"Network error exchanging the Pinterest authorization code for a token: {exc}") from exc

    refresh_token = body.get("refresh_token")
    if not refresh_token:
        raise PermanentError(f"Pinterest token endpoint did not return a refresh_token: {body}")

    return {
        "access_token": body["access_token"],
        "refresh_token": refresh_token,
        "expires_at": _compute_expiry(body.get("expires_in")),
    }
