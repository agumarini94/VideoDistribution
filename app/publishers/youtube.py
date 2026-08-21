"""
Real YouTube publisher (YouTube Data API v3).

Same contract as app/publishers/fake.py: a pure function that knows nothing
about Celery and never prints to screen. It either returns a result dict or
raises TransientError / PermanentError. This means tasks.py doesn't need to
change its retry/DLQ logic to support this publisher — only the routing
(which publisher to call for which platform) changes.

Authorization, two modes (Phase 7 — multi-account):
  - account_credentials given (job has an account_id): built directly from
    that dict via Credentials.from_authorized_user_info, expecting the same
    fields Google's Credentials.to_json() produces (token, refresh_token,
    token_uri, client_id, client_secret, scopes). See
    scripts/authorize_youtube.py --account and scripts/add_account.py for
    how an Account row gets these.
  - account_credentials is None (single-account mode, unchanged since
    Phase 2b): falls back to `token.json` (OAuth2 user credentials with a
    refresh token, generated once via scripts/authorize_youtube.py) and
    `client_secret.json` (an OAuth Client ID downloaded from Google Cloud
    Console) at the project root.

Since publishers are pure and can't write to the database, a refresh that
happens against account_credentials can't be persisted here: publish()
includes the refreshed credentials JSON in its result dict under
"refreshed_credentials" (only when a refresh actually happened), and
app/tasks.py is responsible for saving it back onto the Account row. In
single-account mode, the refreshed token is still written to token.json
directly, same as before.

Credentials aren't provided yet by the client for either mode, so every
code path that depends on them raises a clear PermanentError instead of
crashing, and nothing here executes at import time — credentials are only
read when publish() actually runs.

Proactive refresh (Phase 8): app/tasks.py::refresh_expiring_tokens (a Celery
Beat task) needs to refresh an Account's stored token before it expires,
independent of any publish() call. token_expires_within() and
refresh_stored_credentials() below are the pure helpers it uses — this
module still owns all Google OAuth knowledge (credential shape, refresh
mechanics, how to tell a permanently-invalid refresh token from a transient
failure), the task just decides what to do with the outcome.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from app.exceptions import PermanentError, PublishError, TransientError

# Scope needed to upload videos. Shared with scripts/authorize_youtube.py so
# the token generated there always matches what this publisher expects.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Resolved from this file's location (not the current working directory) so
# the publisher works the same whether it's invoked from a script or a
# Celery worker started from a different directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CLIENT_SECRET_PATH = PROJECT_ROOT / "client_secret.json"
TOKEN_PATH = PROJECT_ROOT / "token.json"

# HTTP 403 reasons that mean "you've hit a quota/rate limit", as opposed to
# other 403s (e.g. terms of service violations) that are genuinely permanent.
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}


def publish(platform: str, payload: dict, account_credentials: dict | None = None) -> dict:
    """
    Uploads a video to YouTube via videos.insert.

    Expected payload keys: video_path, title, description (optional),
    tags (optional), privacy (optional, defaults to "private").

    account_credentials (Phase 7): when given, credentials are built from it
    instead of token.json — see the module docstring for the expected shape
    and for how a refresh gets surfaced back to app/tasks.py via the
    "refreshed_credentials" key of the returned dict.
    """
    try:
        _validate_payload(payload)
        creds, refreshed = _load_credentials(account_credentials)

        video_path = Path(payload["video_path"])
        if not video_path.is_file():
            raise PermanentError(f"Video file not found: {video_path}")

        body = _build_request_body(payload)
        media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)

        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        response = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()

        result = {"platform": "youtube", "external_id": response["id"]}
        if refreshed:
            result["refreshed_credentials"] = json.loads(creds.to_json())
        return result
    except (TransientError, PermanentError):
        raise
    except HttpError as exc:
        raise _classify_http_error(exc) from exc
    except Exception as exc:  # normalize anything unexpected per the publisher contract
        raise TransientError(f"Unexpected error talking to the YouTube API: {exc}") from exc


def _validate_payload(payload: dict) -> None:
    missing = [key for key in ("video_path", "title") if not payload.get(key)]
    if missing:
        raise PermanentError(f"Missing required payload field(s): {', '.join(missing)}")


def _build_request_body(payload: dict) -> dict:
    return {
        "snippet": {
            "title": payload["title"],
            "description": payload.get("description", ""),
            "tags": payload.get("tags", []),
        },
        "status": {
            "privacyStatus": payload.get("privacy", "private"),
        },
    }


def _load_credentials(account_credentials: dict | None) -> tuple[Credentials, bool]:
    """
    Returns (credentials, refreshed) where refreshed indicates a token
    refresh happened during this call, so the caller knows whether it needs
    to persist anything.
    """
    if account_credentials is not None:
        reauth_hint = "re-run scripts/authorize_youtube.py --account <NAME> for this account"
        try:
            creds = Credentials.from_authorized_user_info(account_credentials, SCOPES)
        except (ValueError, KeyError) as exc:
            raise PermanentError(f"Account credentials for YouTube are invalid or incomplete, {reauth_hint}: {exc}") from exc
    else:
        reauth_hint = "re-run scripts/authorize_youtube.py"
        missing = [path.name for path in (CLIENT_SECRET_PATH, TOKEN_PATH) if not path.exists()]
        if missing:
            raise PermanentError(
                "YouTube authorization has not been set up yet (missing: "
                f"{', '.join(missing)}). Place client_secret.json in the project "
                "root and run `python -m scripts.authorize_youtube` to generate token.json."
            )
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except (ValueError, KeyError) as exc:
            raise PermanentError(f"token.json is invalid or incomplete, {reauth_hint}: {exc}") from exc

    refreshed = False
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise PermanentError(f"Failed to refresh YouTube credentials, {reauth_hint}: {exc}") from exc
        refreshed = True
        if account_credentials is None:
            TOKEN_PATH.write_text(creds.to_json())

    if not creds.valid:
        raise PermanentError(f"YouTube credentials are invalid or incomplete, {reauth_hint}.")

    return creds, refreshed


def token_expires_within(credentials: dict, seconds: int) -> bool:
    """
    True if `credentials` (an Account.credentials dict) has no usable
    "expiry", or one that falls within `seconds` from now. Used by
    refresh_expiring_tokens (Phase 8) to decide which accounts need a
    proactive refresh. Treating a missing/unparseable expiry as "needs
    refresh" is deliberate: a token we can't validate is safer to refresh
    than to silently trust.
    """
    expiry_raw = credentials.get("expiry")
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
    Force-refreshes a stored (Account-row) credentials dict and returns the
    refreshed credentials JSON as a dict. Unlike _load_credentials, this
    always calls refresh() rather than checking creds.expired first — the
    caller (refresh_expiring_tokens) already decided a refresh is due via
    token_expires_within.

    Raises PermanentError if the refresh_token is invalid/revoked (Google
    reports this as a non-retryable RefreshError, e.g. "invalid_grant") —
    the caller should deactivate the account and alert a human. Raises
    TransientError for anything else (network blips, a transient error from
    Google's token endpoint) — the caller should just retry on the next
    scheduled run.
    """
    try:
        creds = Credentials.from_authorized_user_info(credentials, SCOPES)
    except (ValueError, KeyError) as exc:
        raise PermanentError(f"Stored YouTube credentials are invalid or incomplete: {exc}") from exc

    try:
        creds.refresh(Request())
    except RefreshError as exc:
        if exc.retryable:
            raise TransientError(f"Transient error refreshing YouTube credentials: {exc}") from exc
        raise PermanentError(f"YouTube refresh token is invalid or has been revoked: {exc}") from exc

    return json.loads(creds.to_json())


def _classify_http_error(exc: HttpError) -> PublishError:
    status = exc.resp.status if exc.resp is not None else None
    reason = _extract_reason(exc)

    if status == 429 or (status is not None and status >= 500):
        return TransientError(f"YouTube API transient error (HTTP {status}, reason={reason}): {exc}")

    if status == 403 and reason in _QUOTA_REASONS:
        return TransientError(f"YouTube API quota/rate limit hit (HTTP 403, reason={reason}): {exc}")

    return PermanentError(f"YouTube API rejected the request (HTTP {status}, reason={reason}): {exc}")


def _extract_reason(exc: HttpError) -> str | None:
    try:
        data = json.loads(exc.content.decode("utf-8"))
        errors = data.get("error", {}).get("errors", [])
        if errors:
            return errors[0].get("reason")
    except (ValueError, AttributeError, UnicodeDecodeError):
        pass
    return None
