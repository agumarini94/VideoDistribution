"""
Real YouTube publisher (YouTube Data API v3).

Same contract as app/publishers/fake.py: a pure function that knows nothing
about Celery and never prints to screen. It either returns a result dict or
raises TransientError / PermanentError. This means tasks.py doesn't need to
change its retry/DLQ logic to support this publisher — only the routing
(which publisher to call for which platform) changes.

Authorization: this module expects a `token.json` (OAuth2 user credentials
with a refresh token, generated once via scripts/authorize_youtube.py) and
`client_secret.json` (an OAuth Client ID downloaded from Google Cloud
Console) at the project root. Credentials aren't provided yet by the client,
so every code path that depends on them raises a clear PermanentError
instead of crashing, and nothing here executes at import time — the files
are only read when publish() actually runs.
"""

import json
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


def publish(platform: str, payload: dict) -> dict:
    """
    Uploads a video to YouTube via videos.insert.

    Expected payload keys: video_path, title, description (optional),
    tags (optional), privacy (optional, defaults to "private").
    """
    try:
        _validate_payload(payload)
        creds = _load_credentials()

        video_path = Path(payload["video_path"])
        if not video_path.is_file():
            raise PermanentError(f"Video file not found: {video_path}")

        body = _build_request_body(payload)
        media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)

        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        response = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()

        return {"platform": "youtube", "external_id": response["id"]}
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


def _load_credentials() -> Credentials:
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
        raise PermanentError(
            f"token.json is invalid or incomplete, re-run scripts/authorize_youtube.py: {exc}"
        ) from exc

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise PermanentError(
                f"Failed to refresh YouTube credentials, re-run scripts/authorize_youtube.py: {exc}"
            ) from exc
        TOKEN_PATH.write_text(creds.to_json())

    if not creds.valid:
        raise PermanentError(
            "YouTube credentials are invalid or incomplete, re-run scripts/authorize_youtube.py."
        )

    return creds


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
