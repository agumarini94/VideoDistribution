"""
Meta (Facebook Pages + Instagram Business) OAuth foundation (Phase 23).

Scope of this phase: OAuth mechanics only — exchanging an authorization code
for tokens, discovering the Pages/Instagram Business account they grant
access to, and keeping the long-lived user token fresh. There is no
publish() here yet (no real content-posting flow implemented against the
Graph API): app/tasks.py has no "facebook"/"instagram" entry in
_PUBLISHERS_BY_PLATFORM, so jobs on those platforms currently fall back to
the fake publisher, same as any platform without a real integration yet.
Building the actual publish flow (POST to /<page_id>/photos, the two-step
IG container/publish flow, etc.) is a natural next phase once this
foundation is exercised against a real App.

Same contract as every other publisher module in this package for the parts
that exist: pure functions, no Celery/DB imports, typed TransientError /
PermanentError from app/exceptions.py. Nothing here executes at import time
(META_APP_ID/META_APP_SECRET are only read inside _app_credentials(), same
pattern as TIKTOK_CLIENT_KEY/SECRET in tiktok.py) — the client's Meta App
doesn't exist yet, so every code path that needs it raises a clear
PermanentError instead of crashing, and this whole module is exercised only
against fully mocked HTTP (tests/test_publisher_meta.py).

Graph API version: v26.0 (the current version at the time this was written —
Meta deprecates versions on a schedule, so this constant will need bumping
eventually; not treated as a env-configurable knob since a version bump is a
deliberate code change, not an environment difference).

OAuth chain (the long-stable, documented Graph API "Facebook Login for
Business" flow — endpoint shapes given directly, not guessed, per the Phase
23 brief):
  1. Browser dialog (scripts/authorize_meta.py opens this):
     GET https://www.facebook.com/v26.0/dialog/oauth
       ?client_id=<APP_ID>&redirect_uri=<URI>&state=<random>&scope=<SCOPES>
     -> redirects back to <URI> with ?code=...&state=...
  2. Code -> short-lived user token: exchange_code_for_user_token()
  3. Short-lived -> long-lived (~60 day) user token: exchange_long_lived_token()
     — this is also how a long-lived token gets REFRESHED later (Meta has no
     separate refresh_token/rotation the way Twitter/TikTok do; you just
     re-exchange the current token before it expires — see
     refresh_stored_credentials() below).
  4. Page discovery + Page tokens: list_pages()
     Page tokens minted from a long-lived user token do not expire in
     practice, per Meta's docs — NOT independently verified against a live
     token yet, flagged here rather than asserted as fact.
  5. Linked Instagram Business account: get_instagram_business_account()

Credential shapes (Account.credentials, see scripts/authorize_meta.py):
  - platform "facebook": {page_id, page_token, page_name, user_token,
    user_token_expires_at}
  - platform "instagram": {ig_user_id, page_id, page_token, user_token,
    user_token_expires_at}
Both shapes carry user_token + page_id, which is all refresh_stored_credentials()
needs — it works unchanged for either platform, and is registered for both
in app/tasks.py::_TOKEN_REFRESH_MODULES_BY_PLATFORM.
"""

import os
from datetime import datetime, timedelta, timezone

import requests

from app.exceptions import PermanentError, PublishError, TransientError

GRAPH_API_VERSION = "v26.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
AUTHORIZE_URL = f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth"

# pages_manage_posts/pages_show_list/pages_read_engagement: Page discovery +
# posting. instagram_basic/instagram_content_publish: IG Business account
# discovery + posting. business_management: needed for Pages owned by a
# Business Manager rather than the authorizing user directly. Most of these
# require App Review before they work for anyone other than the app's own
# admins/developers/testers — unverified until the real App exists.
SCOPES = (
    "pages_manage_posts,pages_show_list,pages_read_engagement,"
    "instagram_basic,instagram_content_publish,business_management"
)

# Graph API error codes worth retrying (rate limiting / transient service
# issues), per developers.facebook.com/docs/graph-api/guides/error-handling:
# 1=API Unknown, 2=API Service (transient), 4=App-level rate limit,
# 17=User-level rate limit, 32=Page-level rate limit, 613=Custom rate limit.
# Not independently verified against live responses yet — same caveat as
# every other publisher's error-code table in this package before real
# credentials exist.
_TRANSIENT_GRAPH_ERROR_CODES = {1, 2, 4, 17, 32, 613}

# OAuthException: the access token is invalid, expired, or was revoked.
_TOKEN_INVALID_GRAPH_ERROR_CODE = 190


def _app_credentials() -> tuple[str, str]:
    app_id = os.getenv("META_APP_ID", "").strip()
    app_secret = os.getenv("META_APP_SECRET", "").strip()
    missing = [name for name, value in (("META_APP_ID", app_id), ("META_APP_SECRET", app_secret)) if not value]
    if missing:
        raise PermanentError(f"Meta app credentials are not configured (missing: {', '.join(missing)}). Set them in .env.")
    return app_id, app_secret


def raise_for_graph_error(
    response: requests.Response, context: str, token_invalid_error_class: type[PublishError] = PermanentError
) -> dict:
    """
    Classifies a Graph API response. Meta reports errors as a top-level
    {"error": {"message", "type", "code", "error_subcode", "fbtrace_id"}}
    alongside a non-2xx HTTP status (unlike TikTok's Content Posting API,
    which sometimes reports errors as HTTP 200 with a nested code — Graph
    API doesn't do that, so the HTTP status alone is enough to know
    something failed).

    Shared across app/publishers/meta.py and app/publishers/facebook.py
    (Phase 24) — not underscore-prefixed, since it's a cross-module contract
    now, not module-private. token_invalid_error_class lets a caller swap
    what a code=190 (OAuthException: invalid/expired/revoked token) error
    raises: meta.py's own OAuth-chain calls (including
    refresh_stored_credentials below) keep the default PermanentError, since
    that's what tells app/tasks.py's proactive refresh path to deactivate
    the account. app/publishers/facebook.py's actual publish-time Graph
    calls pass TokenExpiredError instead, so a token that expired mid-use is
    refreshed and retried once (app/tasks.py::_handle_token_expired) rather
    than dead-lettering the job outright.
    """
    status = response.status_code
    try:
        body = response.json()
    except ValueError as exc:
        if status == 429 or status >= 500:
            raise TransientError(f"Graph API returned a non-JSON response {context} (HTTP {status}): {exc}") from exc
        raise PermanentError(f"Graph API returned a non-JSON response {context} (HTTP {status}): {exc}") from exc

    error = body.get("error")
    if not error:
        return body

    code = error.get("code")
    message = error.get("message", "")
    if status == 429 or status >= 500 or code in _TRANSIENT_GRAPH_ERROR_CODES:
        raise TransientError(f"Graph API transient error {context} (code={code}): {message}")
    if code == _TOKEN_INVALID_GRAPH_ERROR_CODE:
        raise token_invalid_error_class(f"Graph API rejected the request {context} (code={code}): {message}")
    raise PermanentError(f"Graph API rejected the request {context} (code={code}): {message}")


def _compute_expiry(expires_in) -> str | None:
    if not expires_in:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


def exchange_code_for_user_token(code: str, redirect_uri: str) -> dict:
    """
    Step 2 of the OAuth chain: exchanges the authorization code (from the
    dialog redirect) for a SHORT-LIVED user access token.
    GET /oauth/access_token?client_id&client_secret&redirect_uri&code
    -> {"access_token": ..., "token_type": "bearer", "expires_in": ...}

    Used only by scripts/authorize_meta.py — the result is immediately
    upgraded to a long-lived token via exchange_long_lived_token() below;
    nothing else in this project stores a short-lived token.
    """
    app_id, app_secret = _app_credentials()
    response = requests.get(
        f"{GRAPH_API_BASE}/oauth/access_token",
        params={"client_id": app_id, "client_secret": app_secret, "redirect_uri": redirect_uri, "code": code},
        timeout=30,
    )
    body = raise_for_graph_error(response, "exchanging the authorization code")
    return {"access_token": body["access_token"], "expires_in": body.get("expires_in")}


def exchange_long_lived_token(short_lived_token: str) -> dict:
    """
    Step 3: upgrades a short-lived (or an about-to-expire long-lived, see
    refresh_stored_credentials) user token into a fresh long-lived (~60 day)
    one.
    GET /oauth/access_token?grant_type=fb_exchange_token&client_id&
        client_secret&fb_exchange_token=<token>
    -> a new user access token + expires_in.

    This is also how Meta "refreshes" a long-lived token — there is no
    separate refresh_token/rotation like Twitter's or a distinct refresh
    endpoint like TikTok's/YouTube's; you just re-exchange the current token
    before it expires.
    """
    app_id, app_secret = _app_credentials()
    response = requests.get(
        f"{GRAPH_API_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_lived_token,
        },
        timeout=30,
    )
    body = raise_for_graph_error(response, "exchanging for a long-lived token")
    return {"access_token": body["access_token"], "expires_at": _compute_expiry(body.get("expires_in"))}


def list_pages(user_token: str) -> list[dict]:
    """
    Step 4: GET /me/accounts?access_token=<long_lived_user_token>
    -> {"data": [{"id": <page_id>, "name": ..., "access_token": <page_token>,
    ...}]} — every Page the user manages, with a Page-scoped access token
    for each.
    """
    response = requests.get(f"{GRAPH_API_BASE}/me/accounts", params={"access_token": user_token}, timeout=30)
    body = raise_for_graph_error(response, "listing Pages")
    return body.get("data", [])


def get_instagram_business_account(page_id: str, page_token: str) -> str | None:
    """
    Step 5: GET /<page_id>?fields=instagram_business_account&access_token=
    <page_token> -> {"instagram_business_account": {"id": <ig_user_id>}} if
    an Instagram Business (or Creator) account is linked to this Page.

    Returns None (not an error) if the Page has no linked Instagram
    account — that's the common case for a Page that's never been connected
    to an Instagram Business/Creator account, not a failure of this call.
    """
    response = requests.get(
        f"{GRAPH_API_BASE}/{page_id}",
        params={"fields": "instagram_business_account", "access_token": page_token},
        timeout=30,
    )
    body = raise_for_graph_error(response, "looking up the linked Instagram Business account")
    ig_account = body.get("instagram_business_account")
    return ig_account.get("id") if ig_account else None


def token_expires_within(credentials: dict, seconds: int) -> bool:
    """
    True if `credentials` (an Account.credentials dict — platform facebook
    or instagram, both carry user_token_expires_at) has no usable
    user_token_expires_at, or one that falls within `seconds` from now. Same
    contract as the other publishers' token_expires_within
    (youtube.py/tiktok.py/twitter.py), used by
    app/tasks.py::refresh_expiring_tokens — registered there with a 7-day
    window (Meta long-lived tokens last ~60 days, far longer than any other
    platform here, so they need a much wider window than the 45-minute
    default).
    """
    expiry_raw = credentials.get("user_token_expires_at")
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
    Force-refreshes a stored (Account-row) credentials dict — same shape for
    both the facebook and instagram Account platforms, both carrying
    user_token + page_id (see module docstring) — by re-exchanging the
    stored long-lived user_token via exchange_long_lived_token(), then
    re-fetching the Page token for page_id via list_pages() (Page tokens
    don't expire in practice, but are re-fetched here anyway so a Page
    permission change is picked up the same way a token refresh would be,
    rather than needing a separate code path).

    Meta tokens do NOT rotate single-use like Twitter's refresh_token — the
    same long-lived token keeps working to mint new ones, so unlike
    twitter.py there's no "lost the new token, stranded the account" risk
    from calling this more than once against the same starting credentials.

    Returns the updated credentials dict (same keys as the input, with
    user_token/user_token_expires_at/page_token/page_name replaced), or
    raises PermanentError if the stored user_token is invalid/revoked, or if
    the Page is no longer accessible with the refreshed token (removed, or
    access revoked) — either way the caller should deactivate the account
    and alert a human; re-running scripts/authorize_meta.py is the only way
    to recover. Raises TransientError for anything else (network blip, a
    transient Graph API error) — the caller should just retry on the next
    scheduled run.
    """
    user_token = str(credentials.get("user_token", "")).strip()
    page_id = str(credentials.get("page_id", "")).strip()
    missing = [name for name, value in (("user_token", user_token), ("page_id", page_id)) if not value]
    if missing:
        raise PermanentError(f"Stored Meta credentials are missing: {', '.join(missing)}.")

    long_lived = exchange_long_lived_token(user_token)
    new_user_token = long_lived["access_token"]

    pages = list_pages(new_user_token)
    page = next((p for p in pages if str(p.get("id")) == page_id), None)
    if page is None:
        raise PermanentError(
            f"Page {page_id} is no longer accessible with the refreshed user token "
            "(removed, or Page access revoked) — re-run scripts/authorize_meta.py."
        )

    updated = dict(credentials)
    updated["user_token"] = new_user_token
    updated["user_token_expires_at"] = long_lived["expires_at"]
    updated["page_token"] = page.get("access_token", credentials.get("page_token"))
    updated["page_name"] = page.get("name", credentials.get("page_name"))
    return updated
