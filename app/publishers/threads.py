"""
Threads publisher (Phase 29f) — OAuth (no PKCE) + the two-step container/
publish flow, built "ready waiting for credentials" like every other
publisher in this package before real credentials exist: no real Threads
API app has been created yet, so every code path is exercised only against
fully mocked HTTP (tests/test_publisher_threads.py), not a live account.
Same contract as every other publisher: publish() is a pure function, no
Celery/DB imports, and either returns a result dict or raises a typed
TransientError / PermanentError (or TokenExpiredError, a TransientError
subclass).

CRITICAL DESIGN FACT, confirmed against developers.facebook.com/docs/threads
(fetched directly this session, not guessed by analogy to app/publishers/meta.py):
despite being a Meta product, the Threads API is NOT part of the Facebook
Graph API family app/publishers/meta.py/facebook.py/instagram.py share. It
needs its own, separate app registration ("a Meta app created with the
Threads use case" — the docs: "there will be 2 app IDs and app secrets...
use the Threads app ID"), its own OAuth dialog/token endpoints, its own
scopes, and its own token-refresh mechanic. Per this phase's explicit
instruction, this module therefore reads its own THREADS_APP_ID/
THREADS_APP_SECRET env vars directly (_app_credentials(), same pattern as
meta.py's/pinterest.py's _app_credentials()) rather than reusing
META_APP_ID/META_APP_SECRET — and, same as every "ready waiting for
credentials" phase before it, these are left UNSET for now; every code path
that needs them raises a clear PermanentError until a real Threads app
exists.

What IS reused from meta.py: raise_for_graph_error (the response classifier)
— Threads' endpoints live on Meta's Graph-compatible infrastructure
(graph.threads.net) and, per third-party reports and the shared error-code
table, report errors in the same {"error": {"message", "type", "code",
"error_subcode", "fbtrace_id"}} shape alongside a non-2xx HTTP status. This
is a reuse of a response-shape classifier, not a claim that Threads and
Facebook/Instagram share an app or OAuth flow — see above. Not independently
verified against a live Threads error response yet, same caveat every other
publisher's error-code table in this package carries before real
credentials exist.

DOMAIN NAME, DELIBERATELY FLAGGED AS UNVERIFIED (per this phase's explicit
instruction, rather than spending more time trying to pin it down without a
live app to test against): developers.facebook.com's own pages disagree
with each other — some reference "https://www.threads.net/oauth/authorize"
+ "https://graph.threads.net/oauth/access_token" for the OAuth dialog/token
exchange, others reference "https://www.threads.com/oauth/authorize" +
"https://graph.threads.com/oauth/access_token" (Meta appears to be
mid-rebrand from .net to .com for this product's API domain). The
long-lived-token exchange and refresh endpoints were confirmed on
graph.threads.net specifically. This module standardizes on the .net
domain throughout (AUTHORIZE_URL/_TOKEN_URL/_API_BASE below) — if the real
Threads app rejects it, swapping every ".net" here for ".com" is the fix,
flagged here so that's not a mystery later.

OAuth chain (no PKCE — Threads' dialog takes only client_id/redirect_uri/
scope/response_type/state, confirmed against the docs):
  1. Browser dialog: GET https://www.threads.net/oauth/authorize
       ?client_id=<THREADS_APP_ID>&redirect_uri=<URI>&scope=<SCOPES>
       &response_type=code&state=<random>
     -> redirects back to <URI> with ?code=...&state=...
  2. Code -> short-lived access token (~1h): POST https://graph.threads.net/oauth/access_token
     (client_id, client_secret, grant_type=authorization_code, redirect_uri, code)
     -> {"access_token": ..., "user_id": ...}. client_secret is sent in the
     POST body here, NOT HTTP Basic auth (unlike pinterest.py's/twitter.py's
     token endpoints) — confirmed against the docs.
  3. Short-lived -> long-lived (~60 day) token, and ALSO how a long-lived
     token gets refreshed before expiry while it's still valid:
     GET https://graph.threads.net/access_token?grant_type=th_exchange_token
       &client_secret=<THREADS_APP_SECRET>&access_token=<token>
     -> {"access_token": ..., "expires_in": ...}.
  4. Refresh (once the token is at least 24h old, per the docs — not
     specially guarded here, see refresh_stored_credentials below):
     GET https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token
       &access_token=<long_lived_token>
     -> {"access_token": ..., "expires_in": ...}. No app secret needed for
     this call (confirmed against the docs) — unlike step 3. There is no
     separate "refresh_token" concept at all for Threads (unlike Twitter's/
     Pinterest's rotating refresh_token, or TikTok's/YouTube's distinct
     refresh_token): the long-lived access_token itself is what gets
     refreshed/extended in place.
  5. Own Threads user id (no "Pages" concept at all, unlike Meta's Graph
     API — one authorization grants access to exactly one Threads profile):
     GET https://graph.threads.net/v1.0/me?fields=id,username&access_token=<token>
     -> {"id": <threads_user_id>, "username": ...}.

Credentials (Account.credentials, platform "threads", created by the
dashboard's self-service "Connect Threads" OAuth flow — see
dashboard/api.py): {threads_user_id, username, access_token, expires_at}.
No env-var single-account fallback (same posture as tiktok.py/facebook.py/
instagram.py/pinterest.py) — every platform="threads" job needs an Account
row.

Payload contract — per this phase's explicit scope decision, NO carousel
support (text, or a single image, or a single video only):
  {"text": optional str (<=500 chars, Threads' own limit), "media_public_url":
  optional str}. At least one of the two is required (Threads has a
  text-only post type, unlike Instagram). Image vs. video is auto-detected
  from media_public_url's guessed MIME type (mimetypes.guess_type, same
  spirit as facebook.py's/instagram.py's/pinterest.py's _media_kind) — no
  separate "kind" flag. Like Instagram (and unlike Pinterest's video Pins),
  Meta DOWNLOADS the media from a public URL at publish time for BOTH image
  and video ("We will cURL your image/video using the URL provided so it
  must be on a public server", per the docs) — there is no local-file
  upload path here at all; media_public_url must already be staged to
  Cloudflare R2 (app/storage.py::upload_file, see dashboard/api.py).

Endpoint shapes for the actual publish flow (confirmed against
developers.facebook.com/docs/threads, fetched this session), a two-step
container model exactly like instagram.py's (poll included — a community-
reported race condition means firing threads_publish before the container
reaches FINISHED gives an opaque 400):
  1. _create_container(): POST /<THREADS_USER_ID>/threads
     media_type=TEXT&text=<text>, or
     media_type=IMAGE&image_url=<url>&text=<text>, or
     media_type=VIDEO&video_url=<url>&text=<text>
     -> {"id": "<creation_id>"}.
  2. _wait_for_container(): GET /<CREATION_ID>?fields=status,error_message,
     polled every _POLL_INTERVAL_SECONDS up to _POLL_TIMEOUT_SECONDS total.
     IN_PROGRESS -> keep polling; FINISHED -> proceed to step 3; ERROR ->
     PermanentError (using error_message if present) — recreating the
     container from scratch is the only recovery, exactly what happens if
     this job is retried, since no creation_id is persisted between
     attempts. A poll timeout is a TransientError, not PermanentError: the
     container may just need more time, and Celery's retry can pick the
     job up again later. (Only FINISHED/ERROR/IN_PROGRESS were confirmed in
     research for this phase — unlike instagram.py, there's no confirmed
     EXPIRED status here, so anything other than FINISHED/ERROR is treated
     as "still processing" rather than assumed terminal.)
  3. _publish_container(): POST /<THREADS_USER_ID>/threads_publish,
     creation_id=<creation_id> -> {"id": "<thread_id>"}. The post is only
     actually live after this step.

Error classification: every publish-time Graph-shaped call here passes
token_invalid_error_class=TokenExpiredError to raise_for_graph_error, so a
code=190 (OAuthException) error becomes TokenExpiredError instead of
PermanentError — app/tasks.py's existing TokenExpiredError -> refresh ->
retry-once path (_handle_token_expired, Phase 21) works for this unchanged,
same as facebook.py/instagram.py, once app/tasks.py registers this module
in _TOKEN_REFRESH_MODULES_BY_PLATFORM.

NOT implemented here, per this phase's explicit scope decision: carousel
posts (multiple images/videos in one Threads post) — a natural follow-up if
ever needed, modeled on how a future phase might add it, not built now.
"""

import mimetypes
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.publishers.meta import raise_for_graph_error

# See the module docstring's "DOMAIN NAME" note: developers.facebook.com's
# own pages disagree between threads.net and threads.com for the OAuth
# dialog/short-lived-token endpoints. Standardized on .net here; swap every
# occurrence below if a real app rejects it.
AUTHORIZE_URL = "https://www.threads.net/oauth/authorize"
_TOKEN_URL = "https://graph.threads.net/oauth/access_token"
_LONG_LIVED_EXCHANGE_URL = "https://graph.threads.net/access_token"
_REFRESH_URL = "https://graph.threads.net/refresh_access_token"
_API_BASE = "https://graph.threads.net/v1.0"

# Minimal scopes — reading one's own profile id and publishing. Not
# threads_manage_replies/threads_read_replies/threads_manage_insights/
# threads_delete/threads_keyword_search/threads_location_tagging: none of
# those are needed by anything this module does.
_OAUTH_SCOPES = "threads_basic,threads_content_publish"

_CREDENTIAL_FIELDS = ("threads_user_id", "access_token")

_TEXT_MAX_LENGTH = 500

# How often / how long to poll GET /<creation_id> while Threads processes a
# media container, before giving up — same constants/semantics as
# app/publishers/instagram.py's _wait_for_container.
_POLL_INTERVAL_SECONDS = 5
_POLL_TIMEOUT_SECONDS = 300

# Only FINISHED/ERROR/IN_PROGRESS were confirmed for Threads containers this
# session (unlike instagram.py's status_code, which also documents EXPIRED).
_TERMINAL_ERROR_STATUSES = {"ERROR"}


def _app_credentials() -> tuple[str, str]:
    app_id = os.getenv("THREADS_APP_ID", "").strip()
    app_secret = os.getenv("THREADS_APP_SECRET", "").strip()
    missing = [name for name, value in (("THREADS_APP_ID", app_id), ("THREADS_APP_SECRET", app_secret)) if not value]
    if missing:
        raise PermanentError(f"Threads app credentials are not configured (missing: {', '.join(missing)}). Set them in .env.")
    return app_id, app_secret


def _compute_expiry(expires_in) -> str | None:
    if not expires_in:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


def publish(platform: str, payload: dict, account_credentials: dict | None = None) -> dict:
    """
    Publishes a text, image, or video post to Threads: creates a media
    container, polls it until processing finishes, then publishes it. See
    the module docstring for the full payload contract and endpoint shapes.
    """
    try:
        credentials = _resolve_credentials(account_credentials)
        text, media_url = _validate_top_level_payload(payload)
        kind = _media_kind(media_url) if media_url else None

        creation_id = _create_container(credentials, text, media_url, kind)
        _wait_for_container(credentials, creation_id)
        thread_id = _publish_container(credentials, creation_id)

        return {"platform": "threads", "external_id": thread_id}
    except (TransientError, PermanentError):
        raise
    except requests.RequestException as exc:
        raise TransientError(f"Network error talking to the Threads API: {exc}") from exc
    except Exception as exc:  # normalize anything unexpected per the publisher contract
        raise TransientError(f"Unexpected error talking to the Threads API: {exc}") from exc


def _resolve_credentials(account_credentials: dict | None) -> dict:
    # No env-var single-account fallback — same posture as tiktok.py/
    # facebook.py/instagram.py/pinterest.py: there's nowhere sensible for a
    # single Threads access token to live outside an Account row.
    if account_credentials is None:
        raise PermanentError(
            "Threads publishing requires an Account (account_id) — there is no single-account/env-var "
            'fallback for this platform. Connect one via the dashboard\'s self-service "Connect Threads" '
            "button, or scripts/add_account.py."
        )
    values = {field: str(account_credentials.get(field, "")).strip() for field in _CREDENTIAL_FIELDS}
    missing = [field for field in _CREDENTIAL_FIELDS if not values[field]]
    if missing:
        raise PermanentError(f"Threads Account credentials are missing: {', '.join(missing)}.")
    return values


def _validate_top_level_payload(payload: dict) -> tuple[str | None, str | None]:
    text = payload.get("text")
    media_url = payload.get("media_public_url")
    if not text and not media_url:
        raise PermanentError(
            "Payload must include 'text' and/or 'media_public_url' — a Threads post needs at least one "
            "(unlike Instagram, Threads does have a text-only post type)."
        )
    if text and len(text) > _TEXT_MAX_LENGTH:
        raise PermanentError(f"Threads posts are limited to {_TEXT_MAX_LENGTH} characters (got {len(text)}).")
    return text, media_url


def _media_kind(media_url: str) -> str:
    mime, _ = mimetypes.guess_type(media_url)
    if mime is not None and mime.startswith("image/"):
        return "image"
    if mime is not None and mime.startswith("video/"):
        return "video"
    raise PermanentError(f"Unsupported or undetectable media type for {media_url} (guessed mime type: {mime})")


def _create_container(credentials: dict, text: str | None, media_url: str | None, kind: str | None) -> str:
    data = {"access_token": credentials["access_token"]}
    if kind == "image":
        data["media_type"] = "IMAGE"
        data["image_url"] = media_url
    elif kind == "video":
        data["media_type"] = "VIDEO"
        data["video_url"] = media_url
    else:
        data["media_type"] = "TEXT"
    if text:
        data["text"] = text

    response = requests.post(f"{_API_BASE}/{credentials['threads_user_id']}/threads", data=data, timeout=30)
    body = raise_for_graph_error(response, "creating the media container", token_invalid_error_class=TokenExpiredError)
    creation_id = body.get("id")
    if not creation_id:
        raise PermanentError(f"Threads media-container response is missing id: {body}")
    return creation_id


def _get_container_status(credentials: dict, creation_id: str) -> tuple[str, str | None]:
    response = requests.get(
        f"{_API_BASE}/{creation_id}",
        params={"fields": "status,error_message", "access_token": credentials["access_token"]},
        timeout=30,
    )
    body = raise_for_graph_error(response, "checking media container status", token_invalid_error_class=TokenExpiredError)
    status = body.get("status")
    if not status:
        raise PermanentError(f"Threads container-status response is missing status: {body}")
    return status, body.get("error_message")


def _wait_for_container(credentials: dict, creation_id: str) -> None:
    """
    Polls until the container reaches FINISHED (ready to publish), ERROR
    (PermanentError — recreating the container from scratch is the only
    recovery), or _POLL_TIMEOUT_SECONDS elapses (TransientError — the
    container may just need more time; a Celery retry creates a fresh
    container on its next attempt, since no creation_id is persisted
    between attempts). Any other status (confirmed: IN_PROGRESS) keeps
    polling — see the module docstring for why there's no confirmed
    "EXPIRED"-style terminal status for Threads to special-case here.
    """
    elapsed = 0
    while True:
        status, error_message = _get_container_status(credentials, creation_id)
        if status == "FINISHED":
            return
        if status in _TERMINAL_ERROR_STATUSES:
            raise PermanentError(
                f"Threads media container {creation_id} failed processing (status={status}): "
                f"{error_message or 'no error_message given'}"
            )
        if elapsed >= _POLL_TIMEOUT_SECONDS:
            raise TransientError(
                f"Timed out after {_POLL_TIMEOUT_SECONDS}s waiting for Threads media container "
                f"{creation_id} to finish processing (last status={status})"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS


def _publish_container(credentials: dict, creation_id: str) -> str:
    response = requests.post(
        f"{_API_BASE}/{credentials['threads_user_id']}/threads_publish",
        data={"creation_id": creation_id, "access_token": credentials["access_token"]},
        timeout=30,
    )
    body = raise_for_graph_error(response, "publishing the media container", token_invalid_error_class=TokenExpiredError)
    thread_id = body.get("id")
    if not thread_id:
        raise PermanentError(f"Threads publish response is missing id: {body}")
    return thread_id


def token_expires_within(credentials: dict, seconds: int) -> bool:
    """
    True if `credentials` (an Account.credentials dict) has no usable
    "expires_at", or one that falls within `seconds` from now. Same contract
    as every other publisher's token_expires_within in this package, used by
    app/tasks.py::refresh_expiring_tokens.
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
    GET https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token
    &access_token=<token> — no app secret needed for this call (confirmed
    against the docs), unlike the long-lived-token exchange used during the
    initial OAuth flow (exchange_code_for_credentials below).

    Threads has no separate rotating refresh_token the way Twitter's/
    Pinterest's does (see the module docstring, OAuth chain step 4): the
    long-lived access_token itself is what gets refreshed/extended in
    place, so there's no "lost the rotated token, stranded the account"
    risk from calling this more than once, same as meta.py's non-rotating
    tokens.

    Per the docs, a token must be at least 24h old before it can be
    refreshed; this isn't specially guarded here — the 24-hour-old
    constraint is comfortably inside the proactive refresh window this
    module is registered with (app/tasks.py), so in practice this call is
    never attempted against a too-young token. If Threads ever rejects one
    anyway, that surfaces as whatever error raise_for_graph_error classifies
    it as (most likely PermanentError, same as any other rejected request),
    which the caller handles like any other refresh failure.

    Returns the updated credentials dict (same keys as the input, with
    access_token/expires_at replaced), or raises PermanentError if the
    stored access_token is invalid/revoked (caller should deactivate the
    account and alert a human — re-connecting via the dashboard's
    self-service "Connect Threads" button is the only way to recover) or
    TransientError for anything else (network blip, a transient error from
    Threads' token endpoint, or a response missing the refreshed
    access_token).
    """
    access_token = str(credentials.get("access_token", "")).strip()
    if not access_token:
        raise PermanentError("Stored Threads credentials are missing: access_token.")

    response = requests.get(
        _REFRESH_URL, params={"grant_type": "th_refresh_token", "access_token": access_token}, timeout=30
    )
    body = raise_for_graph_error(response, "refreshing token")

    new_access_token = body.get("access_token")
    if not new_access_token:
        raise TransientError(f"Threads refresh endpoint did not return a new access_token: {body}")

    updated = dict(credentials)
    updated["access_token"] = new_access_token
    updated["expires_at"] = _compute_expiry(body.get("expires_in"))
    return updated


def build_authorization_url(redirect_uri: str, state: str) -> str:
    """
    Builds Threads' OAuth consent-screen URL — the in-browser counterpart to
    a CLI authorize script (Threads has none, same as Pinterest's; this is
    the only authorization flow this platform ever gets). No PKCE
    (confirmed: the docs list only client_id/redirect_uri/scope/
    response_type/state), so the caller (dashboard/api.py) needs no
    verifier/challenge pair — state only carries client_id/user_id, same
    shape as YouTube's/Meta's/Pinterest's.

    Reads THREADS_APP_ID from the environment (app-level, no Account row
    exists yet at this point in the flow — same reasoning as meta.py's/
    pinterest.py's build_authorization_url reading their own app
    credentials directly); raises PermanentError if unset (or if
    THREADS_APP_SECRET is also unset, via _app_credentials() — the secret
    isn't used here, but requiring both up front matches every other
    credential check in this module and fails clearly before a redirect
    that would later dead-end at the token exchange anyway).
    """
    app_id, _app_secret = _app_credentials()
    query = urlencode(
        {"client_id": app_id, "redirect_uri": redirect_uri, "scope": _OAUTH_SCOPES, "response_type": "code", "state": state}
    )
    return f"{AUTHORIZE_URL}?{query}"


def _exchange_short_lived_token(code: str, redirect_uri: str) -> str:
    app_id, app_secret = _app_credentials()
    response = requests.post(
        _TOKEN_URL,
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    body = raise_for_graph_error(response, "exchanging the authorization code")
    access_token = body.get("access_token")
    if not access_token:
        raise PermanentError(f"Threads token endpoint response is missing access_token: {body}")
    return access_token


def _exchange_long_lived_token(short_lived_token: str) -> dict:
    _app_id, app_secret = _app_credentials()
    response = requests.get(
        _LONG_LIVED_EXCHANGE_URL,
        params={"grant_type": "th_exchange_token", "client_secret": app_secret, "access_token": short_lived_token},
        timeout=30,
    )
    body = raise_for_graph_error(response, "exchanging for a long-lived token")
    access_token = body.get("access_token")
    if not access_token:
        raise PermanentError(f"Threads long-lived-token exchange response is missing access_token: {body}")
    return {"access_token": access_token, "expires_at": _compute_expiry(body.get("expires_in"))}


def _fetch_own_profile(access_token: str) -> tuple[str, str | None]:
    response = requests.get(f"{_API_BASE}/me", params={"fields": "id,username", "access_token": access_token}, timeout=30)
    body = raise_for_graph_error(response, "looking up the authorized Threads profile")
    threads_user_id = body.get("id")
    if not threads_user_id:
        raise PermanentError(f"Threads profile lookup response is missing id: {body}")
    return str(threads_user_id), body.get("username")


def exchange_code_for_credentials(code: str, redirect_uri: str) -> dict:
    """
    Exchanges an authorization code (from the browser redirect back after
    build_authorization_url's consent screen) for a full credentials dict —
    fanning in three calls (short-lived token -> long-lived token -> own
    profile id), unlike pinterest.py's/twitter.py's single-call
    exchange_code_for_credentials, because Threads' token exchange alone
    doesn't carry a usable long-lived token or confirm the profile id this
    module needs for every subsequent API call.

    Unlike refresh_stored_credentials (called from the proactive Beat task,
    where distinguishing transient vs. permanent matters so the caller can
    decide whether to retry automatically), this is a one-shot interactive
    flow driven by a human sitting at the consent screen — any failure just
    means clicking "Connect" again — so every failure here normalizes to
    PermanentError, same contract as app/publishers/youtube.py's/
    twitter.py's/tiktok.py's/pinterest.py's exchange_code_for_credentials.
    """
    try:
        short_lived_token = _exchange_short_lived_token(code, redirect_uri)
        long_lived = _exchange_long_lived_token(short_lived_token)
        threads_user_id, username = _fetch_own_profile(long_lived["access_token"])
    except (TransientError, PermanentError) as exc:
        raise PermanentError(f"Failed to exchange the Threads authorization code for a token: {exc}") from exc
    except requests.RequestException as exc:
        raise PermanentError(f"Network error exchanging the Threads authorization code for a token: {exc}") from exc

    return {
        "threads_user_id": threads_user_id,
        "username": username,
        "access_token": long_lived["access_token"],
        "expires_at": long_lived["expires_at"],
    }
