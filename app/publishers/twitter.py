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
"""

import os

import tweepy

from app.exceptions import PermanentError, PublishError, TransientError


def publish(platform: str, payload: dict, account_credentials: dict | None = None) -> dict:
    """
    Posts a tweet via POST /2/tweets.

    Expected payload keys: text (str, required). Media isn't supported yet
    (comes later, via app/storage.py once R2 is wired in).
    """
    try:
        _validate_payload(payload)
        client = _build_client(account_credentials)
        response = client.create_tweet(text=payload["text"])
        return {"platform": "twitter", "external_id": response.data["id"]}
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


def _validate_payload(payload: dict) -> None:
    if not payload.get("text"):
        raise PermanentError("Missing required payload field: text")


def _build_client(account_credentials: dict | None) -> tweepy.Client:
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

    return tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )


def _classify_http_error(exc: tweepy.HTTPException) -> PublishError:
    status = exc.response.status_code if exc.response is not None else None

    if status == 429 or (status is not None and status >= 500):
        return TransientError(f"X API transient error (HTTP {status}): {exc}")

    if status in (401, 403):
        return PermanentError(f"X API rejected the credentials (HTTP {status}): {exc}")

    return PermanentError(f"X API rejected the request (HTTP {status}): {exc}")
