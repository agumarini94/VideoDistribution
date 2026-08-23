"""
Real TikTok publisher (Content Posting API v2) — Sandbox mode.

Same contract as the other publishers in this package: publish() is a pure
function that knows nothing about Celery or the database. It either returns
a result dict or raises TransientError / PermanentError.

Sandbox limitation (Phase 10): the app's TikTok Developer Portal review has
not passed yet, so only the `video.upload` scope is granted — `video.publish`
(Direct Post) is gated behind app review. This means every upload lands as a
draft in the target account's TikTok inbox (the user still has to open the
TikTok app and manually post it) rather than being published directly. This
also only works against Sandbox target accounts registered in the portal
until review passes.

Inbox upload vs. Direct Post: the two flows differ only in which init
endpoint is called and what the request body contains — the chunked upload
mechanics after that point are identical. Everything endpoint/body-specific
is isolated in _init_upload/_build_init_body below, with the switch marked
inline, so moving to Direct Post once video.publish is approved is a small,
contained change.

Auth: bearer access_token from account_credentials (an Account.credentials
dict: access_token, refresh_token, expires_at, open_id, scope — see
scripts/authorize_tiktok.py). Unlike youtube.py/twitter.py, there is no
single-account env-var fallback: TikTok has no equivalent of token.json or
X_ACCESS_TOKEN in this project, so every tiktok job needs an account_id.
App-level client_key/client_secret (from the TikTok Developer Portal) always
come from TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET, since they belong to the
app, not to an individual account — only used for token refresh/exchange,
not for publish() itself.

Credentials aren't provided by the client yet for real accounts (sandbox
target accounts are self-registered per developer), so every code path that
depends on them raises a clear PermanentError instead of crashing, and
nothing here executes at import time.

Proactive refresh (Phase 8 pattern): token_expires_within() and
refresh_stored_credentials() mirror the helpers in youtube.py so
app/tasks.py::refresh_expiring_tokens can manage TikTok accounts the same
way — register this module in _TOKEN_REFRESH_MODULES_BY_PLATFORM.
exchange_authorization_code() is the one-time counterpart used only by
scripts/authorize_tiktok.py, keeping all TikTok OAuth mechanics (credential
shape, endpoints, error classification) in this module rather than the
script.
"""

import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from app.exceptions import PermanentError, TransientError

# --- OAuth (shared by publish()'s bearer auth, refresh, and the one-time
# authorization script) -------------------------------------------------

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
# Sandbox only grants video.upload; user.info.basic is requested alongside it
# because TikTok's Login Kit requires at least one basic-info scope in the
# same authorization.
SCOPES = "user.info.basic,video.upload"

_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}

# error codes from the /v2/oauth/token/ endpoint that are worth retrying
# (transient on TikTok's side); everything else (e.g. invalid_grant for a
# revoked/expired refresh token) is permanent.
_TRANSIENT_TOKEN_ERROR_CODES = {"server_error", "temporarily_unavailable"}

# --- Content Posting API (inbox upload) ---------------------------------

# Sandbox-only endpoint: uploads land as a draft in the user's TikTok inbox
# instead of being published. When video.publish is approved, switch to
# Direct Post by pointing _init_upload at this URL and adding a "post_info"
# object (title, privacy_level, disable_comment, ...) to the request body
# built in _build_init_body — the chunked upload step below doesn't change.
_INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
_DIRECT_POST_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"  # not used yet, see above

# TikTok's chunking rules: chunk_size must be between 5 MB and 64 MB, except
# when the whole video is under 5 MB, which uploads as a single chunk equal
# to its full size.
_MIN_CHUNK_SIZE = 5 * 1024 * 1024
_MAX_CHUNK_SIZE = 64 * 1024 * 1024

# error codes from the Content Posting API (post/init) worth retrying;
# everything else (invalid_param, access_token_invalid, spam_risk_*, ...) is
# permanent.
_TRANSIENT_API_ERROR_CODES = {"rate_limit_exceeded", "internal_error"}


def publish(platform: str, payload: dict, account_credentials: dict | None = None) -> dict:
    """
    Uploads a video to TikTok via the inbox-upload flow: POST .../init/ to
    get a publish_id + upload_url, then PUT the file to upload_url in
    chunks. The video lands as a draft in the target account's TikTok inbox
    (Sandbox limitation — see module docstring), it is not published
    automatically.

    Expected payload keys: video_path (required). `title` is accepted but
    unused in inbox mode (TikTok assigns no caption until the user manually
    posts the draft from the app) — it becomes meaningful again once this
    switches to Direct Post.
    """
    try:
        _validate_payload(payload)
        access_token = _resolve_access_token(account_credentials)

        video_path = Path(payload["video_path"])
        if not video_path.is_file():
            raise PermanentError(f"Video file not found: {video_path}")

        video_size = video_path.stat().st_size
        chunk_size, total_chunk_count = _compute_chunks(video_size)

        publish_id, upload_url = _init_upload(access_token, video_size, chunk_size, total_chunk_count)
        _upload_chunks(upload_url, video_path, video_size, chunk_size, total_chunk_count)

        return {"platform": "tiktok", "external_id": publish_id}
    except (TransientError, PermanentError):
        raise
    except requests.RequestException as exc:
        raise TransientError(f"Network error talking to the TikTok API: {exc}") from exc
    except Exception as exc:  # normalize anything unexpected per the publisher contract
        raise TransientError(f"Unexpected error talking to the TikTok API: {exc}") from exc


def _validate_payload(payload: dict) -> None:
    if not payload.get("video_path"):
        raise PermanentError("Missing required payload field: video_path")


def _resolve_access_token(account_credentials: dict | None) -> str:
    if account_credentials is None:
        raise PermanentError(
            "TikTok has no single-account fallback: this job needs an account_id "
            "pointing at an Account row created via "
            "`python -m scripts.authorize_tiktok --account NAME`."
        )
    access_token = str(account_credentials.get("access_token", "")).strip()
    if not access_token:
        raise PermanentError(
            "TikTok Account credentials are missing access_token, re-run "
            "scripts/authorize_tiktok.py --account <NAME>."
        )
    return access_token


def _compute_chunks(video_size: int) -> tuple[int, int]:
    """
    Picks the largest allowed chunk size (64 MB) to minimize the number of
    PUT requests, letting the last chunk absorb the remainder. See the
    module-level constants for TikTok's min/max chunk-size rule.
    """
    if video_size <= _MIN_CHUNK_SIZE:
        return video_size, 1
    chunk_size = _MAX_CHUNK_SIZE
    total_chunk_count = math.ceil(video_size / chunk_size)
    return chunk_size, total_chunk_count


def _build_init_body(video_size: int, chunk_size: int, total_chunk_count: int) -> dict:
    return {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunk_count,
        }
    }


def _init_upload(access_token: str, video_size: int, chunk_size: int, total_chunk_count: int) -> tuple[str, str]:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}
    body = _build_init_body(video_size, chunk_size, total_chunk_count)

    response = requests.post(_INBOX_INIT_URL, headers=headers, json=body, timeout=30)
    result = _raise_for_api_error(response, "initializing upload")

    data = result.get("data") or {}
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")
    if not publish_id or not upload_url:
        raise PermanentError(f"TikTok init response is missing publish_id/upload_url: {result}")
    return publish_id, upload_url


def _upload_chunks(upload_url: str, video_path: Path, video_size: int, chunk_size: int, total_chunk_count: int) -> None:
    with video_path.open("rb") as f:
        for index in range(total_chunk_count):
            start = index * chunk_size
            end = min(start + chunk_size, video_size) - 1
            chunk = f.read(end - start + 1)
            headers = {
                "Content-Range": f"bytes {start}-{end}/{video_size}",
                "Content-Type": "video/mp4",
                "Content-Length": str(len(chunk)),
            }
            response = requests.put(upload_url, headers=headers, data=chunk, timeout=120)
            _raise_for_upload_status(response, index, total_chunk_count)


def _raise_for_upload_status(response: requests.Response, index: int, total_chunk_count: int) -> None:
    status = response.status_code
    if status in (200, 201, 206):
        return
    if status == 429 or status >= 500:
        raise TransientError(
            f"TikTok transient error uploading chunk {index + 1}/{total_chunk_count} (HTTP {status}): {response.text}"
        )
    raise PermanentError(f"TikTok rejected chunk {index + 1}/{total_chunk_count} (HTTP {status}): {response.text}")


def _raise_for_api_error(response: requests.Response, context: str) -> dict:
    """
    Classifies a Content Posting API response. TikTok answers with HTTP 200
    even for most logical errors, putting the real status in a nested
    body["error"]["code"] (distinct from the /v2/oauth/token/ shape handled
    by _raise_for_token_error) — so both the HTTP status and the body need
    checking.
    """
    status = response.status_code
    if status == 429 or status >= 500:
        raise TransientError(f"TikTok API transient error {context} (HTTP {status}): {response.text}")
    if status in (401, 403):
        raise PermanentError(f"TikTok API rejected the credentials {context} (HTTP {status}): {response.text}")
    if status >= 400:
        raise PermanentError(f"TikTok API rejected the request {context} (HTTP {status}): {response.text}")

    try:
        body = response.json()
    except ValueError as exc:
        raise TransientError(f"TikTok API returned a non-JSON response {context}: {exc}") from exc

    code = (body.get("error") or {}).get("code", "ok")
    if code and code != "ok":
        message = (body.get("error") or {}).get("message", "")
        if code in _TRANSIENT_API_ERROR_CODES:
            raise TransientError(f"TikTok API transient error {context} (code={code}): {message}")
        raise PermanentError(f"TikTok API rejected the request {context} (code={code}): {message}")

    return body


def _raise_for_token_error(response: requests.Response, context: str) -> dict:
    """
    Classifies a /v2/oauth/token/ response. Unlike the Content Posting API,
    this endpoint reports errors as top-level "error"/"error_description"
    strings — a different shape from _raise_for_api_error, handled
    separately on purpose rather than guessing a shared parser.
    """
    status = response.status_code
    try:
        body = response.json()
    except ValueError as exc:
        raise TransientError(f"TikTok token endpoint returned a non-JSON response {context}: {exc}") from exc

    error = body.get("error")
    if not error:
        return body

    description = body.get("error_description", "")
    if status == 429 or status >= 500 or error in _TRANSIENT_TOKEN_ERROR_CODES:
        raise TransientError(f"TikTok token endpoint transient error {context} ({error}): {description}")
    raise PermanentError(f"TikTok token endpoint rejected the request {context} ({error}): {description}")


def _app_credentials() -> tuple[str, str]:
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    missing = [
        name
        for name, value in (("TIKTOK_CLIENT_KEY", client_key), ("TIKTOK_CLIENT_SECRET", client_secret))
        if not value
    ]
    if missing:
        raise PermanentError(f"TikTok app credentials are not configured (missing: {', '.join(missing)}). Set them in .env.")
    return client_key, client_secret


def _compute_expiry(expires_in) -> str | None:
    if not expires_in:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


def exchange_authorization_code(code: str, redirect_uri: str, code_verifier: str) -> dict:
    """
    Exchanges an OAuth "authorization code" (from the redirect after the
    user approves access) for an access/refresh token pair. Used only by
    scripts/authorize_tiktok.py, the interactive one-time setup step — not
    by publish() or the proactive refresh path.

    code_verifier is the PKCE verifier generated alongside the
    code_challenge sent to the authorize URL (TikTok's OAuth requires PKCE,
    see scripts/authorize_tiktok.py) — TikTok recomputes S256(verifier) and
    checks it matches the challenge it received earlier, which is what
    proves this exchange came from the same client that started the flow.
    """
    client_key, client_secret = _app_credentials()
    response = requests.post(
        TOKEN_URL,
        headers=_FORM_HEADERS,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=30,
    )
    body = _raise_for_token_error(response, "exchanging authorization code")
    return {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "open_id": body.get("open_id"),
        "scope": body.get("scope"),
        "expires_at": _compute_expiry(body.get("expires_in")),
    }


def token_expires_within(credentials: dict, seconds: int) -> bool:
    """
    True if `credentials` (an Account.credentials dict) has no usable
    "expires_at", or one that falls within `seconds` from now. Same
    semantics as youtube.py's token_expires_within, used by
    refresh_expiring_tokens (Phase 8) to decide which accounts need a
    proactive refresh.
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
    Force-refreshes a stored (Account-row) credentials dict via
    POST /v2/oauth/token/ (grant_type=refresh_token) and returns the
    refreshed credentials dict. Raises PermanentError if the refresh_token
    is invalid/revoked (e.g. error="invalid_grant") — the caller
    (refresh_expiring_tokens) should deactivate the account and alert a
    human. Raises TransientError for anything else (network blips, a
    transient error from TikTok's token endpoint) — the caller should just
    retry on the next scheduled run.
    """
    refresh_token = str(credentials.get("refresh_token", "")).strip()
    if not refresh_token:
        raise PermanentError("Stored TikTok credentials are missing refresh_token.")

    client_key, client_secret = _app_credentials()
    response = requests.post(
        TOKEN_URL,
        headers=_FORM_HEADERS,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    body = _raise_for_token_error(response, "refreshing token")

    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", refresh_token),
        "open_id": body.get("open_id", credentials.get("open_id")),
        "scope": body.get("scope", credentials.get("scope")),
        "expires_at": _compute_expiry(body.get("expires_in")),
    }
