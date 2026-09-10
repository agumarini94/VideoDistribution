"""
Read-only monitoring dashboard for the distribution engine, plus two write
actions: retrying a failed job, and receiving TikTok's webhook (Phase 10b).

Design decision: this app only ever imports from app/ (SessionLocal, Job,
JobStatus, WebhookEvent, publish_job, handle_tiktok_webhook_event, and the
app.webhooks.tiktok verification/parsing helpers) and never the other way
around, so the dashboard stays a bolt-on layer that the engine has no
knowledge of — same pattern retry_job already uses for publish_job.delay.
The webhook route itself does no business logic: it verifies the
signature, stores the raw event, and dispatches a Celery task
(handle_tiktok_webhook_event) to do the rest, exactly so the actual
matching/alerting logic lives in app/tasks.py, not here.
"""

import base64
import hashlib
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import storage
from app.auth import (
    create_oauth_state_token,
    create_session_token,
    hash_password,
    verify_oauth_state_token,
    verify_password,
    verify_session_token,
)
from app.db import SessionLocal
from app.exceptions import PermanentError, PublishError, StorageNotConfiguredError
from app.models import Account, Client, Job, JobStatus, User, WebhookEvent
from app.publishers import meta as meta_publisher
from app.publishers import tiktok as tiktok_publisher
from app.publishers import twitter as twitter_publisher
from app.publishers import youtube as youtube_publisher
from app.tasks import handle_tiktok_webhook_event, publish_job
from app.webhooks import tiktok as tiktok_webhooks
from scripts.add_account import upsert_account

logger = logging.getLogger(__name__)

app = FastAPI(title="Distribution Engine Dashboard")

# TIKTOK_WEBHOOK_SKIP_SIGNATURE=1 is a local-curl-testing-only escape hatch
# (see app/webhooks/tiktok.py) — it must never be set in production, so
# this warning fires loudly once at process startup, not just in a log
# line that could scroll by unnoticed.
if tiktok_webhooks.verification_skipped():
    logger.warning(
        "\n"
        + "!" * 78
        + "\nTIKTOK_WEBHOOK_SKIP_SIGNATURE=1: POST /webhooks/tiktok signature "
        "verification is DISABLED.\nThis accepts ANY request as if it came from "
        "TikTok. Local curl testing ONLY — never set this in production.\n"
        + "!" * 78
    )

# HTTP Basic auth, pre-deploy hardening (not part of the original spec).
# Both DASHBOARD_USERNAME and DASHBOARD_PASSWORD must be set for auth to be
# enforced; if either is missing the app still runs (local dev convenience)
# but logs a loud warning, same style as the TIKTOK_WEBHOOK_SKIP_SIGNATURE
# one above. Unchanged by Phase 28 — this remains the "ops" credential (curl,
# scripts, /docs), always granting full/admin access, independent of the
# User table below.
_DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "").strip()
_DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
_AUTH_ENABLED = bool(_DASHBOARD_USERNAME and _DASHBOARD_PASSWORD)

if not _AUTH_ENABLED:
    logger.warning(
        "\n"
        + "!" * 78
        + "\nDASHBOARD_USERNAME / DASHBOARD_PASSWORD not both set: the dashboard "
        "is UNPROTECTED.\nAnyone who can reach this process can read job/account "
        "data and trigger retries.\nLocal dev ONLY — never run without both set "
        "in production.\n"
        + "!" * 78
    )

# Paths exempted from auth entirely:
# - the TikTok webhook: TikTok's servers POST here directly and can't
#   supply dashboard credentials — the request's own signature
#   (TikTok-Signature header, verified in tiktok_webhook below) is its auth.
# - /health: Fly's http_check probes this before the machine is considered
#   up; it never carries credentials either, and exposes no sensitive data.
_WEBHOOK_PATH = "/webhooks/tiktok"
_HEALTH_PATH = "/health"
_NO_AUTH_PATHS = {_WEBHOOK_PATH, _HEALTH_PATH}

# FastAPI's auto-generated docs. Kept behind a real WWW-Authenticate
# challenge (see enforce_auth below) since these are opened directly in a
# browser tab, not fetched by the SPA's own JS — the old native Basic-Auth
# popup UX is still appropriate here.
_DOC_PATHS = {"/docs", "/redoc", "/openapi.json"}

# Auth endpoints below (Phase 28) are the one place an unauthenticated
# visitor is *expected* to hit a /api/* route — login/register have to be
# reachable before any session exists, and /me is how the SPA silently
# checks "am I logged in?" on load without forcing a 401 round trip.
#
# /api/oauth/youtube/callback (Phase 29a), /api/oauth/twitter/callback
# (Phase 29b), /api/oauth/tiktok/callback (Phase 29c) and
# /api/oauth/meta/callback (Phase 29d, same reasoning) are the other public
# ones: the platform redirects the browser here with no session cookie at
# all, so all four have to be public too — their own security comes from
# verifying the signed "state" param (see
# youtube_oauth_callback/twitter_oauth_callback/tiktok_oauth_callback/
# meta_oauth_callback below), not from enforce_auth. Their sibling /start
# routes are deliberately NOT here — those still require a real client_user
# session, gated normally.
_PUBLIC_API_PATHS = {
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/oauth/youtube/callback",
    "/api/oauth/twitter/callback",
    "/api/oauth/tiktok/callback",
    "/api/oauth/meta/callback",
}


def _is_protected(path: str) -> bool:
    """
    Phase 28 change: the static SPA shell (index.html/JS/CSS) is no longer
    gated at all — it contains no secrets, and gating it meant an
    unauthenticated visitor got the browser's native Basic-Auth popup
    before ever seeing this app's own Login screen. Only /docs & friends
    and the real /api/* data routes (minus the public auth ones above)
    require a credential now.
    """
    if path in _NO_AUTH_PATHS:
        return False
    if path in _DOC_PATHS:
        return True
    if path.startswith("/api/"):
        return path not in _PUBLIC_API_PATHS
    return False


_basic_auth = HTTPBasic(auto_error=False)


def _credentials_valid(credentials: HTTPBasicCredentials | None) -> bool:
    if credentials is None:
        return False
    # Both comparisons always run (no short-circuit on username) so a
    # mismatched username doesn't skip the password comparison and leak
    # timing information about which part was wrong.
    valid_username = secrets.compare_digest(credentials.username, _DASHBOARD_USERNAME)
    valid_password = secrets.compare_digest(credentials.password, _DASHBOARD_PASSWORD)
    return valid_username and valid_password


@dataclass(frozen=True)
class AuthContext:
    """
    Who's making this request, resolved once per request by enforce_auth
    and stashed on request.state.auth. role is "admin", "client_user", or
    "anonymous". client_id is only meaningful for "client_user" — every
    scoped endpoint below filters by it. user_id/email are None for the
    Basic-Auth ("ops") admin path, since that grants access without any
    User row existing.
    """

    role: str
    client_id: int | None
    user_id: int | None
    email: str | None


ADMIN_AUTH = AuthContext(role="admin", client_id=None, user_id=None, email=None)
ANONYMOUS_AUTH = AuthContext(role="anonymous", client_id=None, user_id=None, email=None)

# Cookie carrying the signed session token (app/auth.py). httponly so page
# JS can never read it (XSS can't exfiltrate it), SameSite=Lax so it's never
# sent on a cross-site request (the main mitigation against CSRF for this
# phase — see CLAUDE.md Phase 28 for why a dedicated CSRF token was judged
# unnecessary on top of that for a same-origin SPA).
_SESSION_COOKIE_NAME = "de_session"
_SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600  # matches app/auth.py::SESSION_MAX_AGE_SECONDS

# Cookies marked Secure are only ever sent over HTTPS — required in
# production (Fly), but a plain "true" default would silently break local
# dev over http://localhost. Defaults to secure and warns loudly if turned
# off, same posture as every other "safe by default, opt out explicitly"
# setting in this file.
_SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").strip().lower() not in ("0", "false", "no")
if not _SESSION_COOKIE_SECURE:
    logger.warning(
        "\n"
        + "!" * 78
        + "\nSESSION_COOKIE_SECURE=false: dashboard session cookies will be sent "
        "over plain HTTP.\nLocal dev ONLY — never set this in production "
        "(the cookie could be intercepted\non the network).\n"
        + "!" * 78
    )


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        _SESSION_COOKIE_NAME,
        token,
        max_age=_SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_SESSION_COOKIE_SECURE,
        path="/",
    )


async def _resolve_auth(request: Request) -> AuthContext:
    """
    Tries, in order: Basic Auth (env-var "ops" admin, unchanged) -> session
    cookie (Phase 28 User table, either role) -> anonymous. Runs on every
    request regardless of whether the path is gated, so GET /api/auth/me
    can report real identity for a path that itself requires no auth.
    """
    if not _AUTH_ENABLED:
        # Dev-mode bypass, unchanged from before Phase 28: with no
        # DASHBOARD_USERNAME/PASSWORD configured the whole dashboard was
        # already wide open, so every request acts as admin.
        return ADMIN_AUTH

    credentials = await _basic_auth(request)
    if _credentials_valid(credentials):
        return ADMIN_AUTH

    token = request.cookies.get(_SESSION_COOKIE_NAME)
    if not token:
        return ANONYMOUS_AUTH

    user_id = verify_session_token(token)
    if user_id is None:
        return ANONYMOUS_AUTH

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
    finally:
        db.close()

    # Re-checked live (not encoded in the token) so a since-rejected/
    # deleted account loses access on its very next request, not just the
    # next time it happens to re-authenticate.
    if user is None or not user.is_approved:
        return ANONYMOUS_AUTH

    return AuthContext(role=user.role, client_id=user.client_id, user_id=user.id, email=user.email)


def _get_auth(request: Request | None) -> AuthContext:
    """
    Every scoped route below takes `request: Request | None = None` instead
    of a Depends-based dependency, specifically so it stays callable
    directly with no request at all (every existing test in this suite,
    e.g. tests/test_dashboard_media_staging.py, calls route functions this
    way, bypassing the HTTP layer entirely — Depends objects don't resolve
    outside of a real ASGI call). A direct call with no request is treated
    as a trusted/internal admin caller, matching this project's existing
    convention of everything being callable directly in tests.
    """
    if request is None:
        return ADMIN_AUTH
    return getattr(request.state, "auth", ADMIN_AUTH)


def _require_admin(request: Request | None) -> AuthContext:
    auth = _get_auth(request)
    if auth.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return auth


# A single HTTP middleware (rather than a per-route Depends) so this covers
# every route uniformly without having to remember to wire it into each one
# individually. Registered before CORSMiddleware below so CORS ends up as
# the outer layer and keeps handling preflight OPTIONS requests (which never
# carry credentials) without hitting auth.
@app.middleware("http")
async def enforce_auth(request: Request, call_next):
    request.state.auth = await _resolve_auth(request)

    if not _is_protected(request.url.path):
        return await call_next(request)

    if request.state.auth.role == "anonymous":
        # WWW-Authenticate only on the /docs family (see _DOC_PATHS): it's
        # what triggers a browser's native Basic-Auth popup, which is
        # wanted there but must NOT fire on the SPA's own /api/* fetch()
        # calls — that would interrupt a client_user's login screen with an
        # unrelated OS-level credential prompt.
        headers = {"WWW-Authenticate": "Basic"} if request.url.path in _DOC_PATHS else {}
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing credentials"},
            headers=headers,
        )
    return await call_next(request)


# Dev-only CORS: the frontend is normally served by this same process (see
# the StaticFiles mount below), but allowing localhost lets it also be
# opened from a separate dev server (e.g. `vite`/`live-server`) on another
# port during frontend iteration.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Upload target for POST /api/jobs (see below): same project-root "uploads/"
# directory scripts/enqueue_tiktok_test.py and enqueue_youtube_test.py point
# a job's payload["video_path"] at, just written to by this process instead
# of passed in by hand. Excluded from git (see .gitignore).
_UPLOADS_DIR = Path(__file__).parent.parent / "uploads"

# Only platforms scripts/enqueue_*_test.py already know how to build a
# payload for. Other platforms (fake, ...) aren't offered through this flow.
_SUPPORTED_PLATFORMS = {"youtube", "tiktok", "twitter", "facebook", "instagram"}

# Platforms whose job payload is built from a single required video file
# upload (video_path). Twitter (Phase 21), Facebook (Phase 24) and Instagram
# (Phase 25) aren't among these: their payloads are text-first (twitter/
# facebook) or media-required-but-not-local (instagram) — see create_job
# below.
_VIDEO_UPLOAD_PLATFORMS = {"youtube", "tiktok"}

# Platforms with no single-account/env-var fallback (app/publishers/*.py
# owns this rule; duplicated here only enough to give a friendlier 400
# instead of letting the job get created and fail later in the worker).
_ACCOUNT_REQUIRED_PLATFORMS = {"tiktok", "facebook", "instagram"}


def _extract_caption(payload: dict) -> str | None:
    """
    Best-effort human-readable label for a job's content, for screens that
    show one line per post (Queue, Calendar, Approvals — scheduler UI,
    Phase 26). Payload shapes vary by platform (see CLAUDE.md): "text" for
    twitter/facebook/instagram, "title" for youtube/tiktok, "thread" (a list,
    no top-level "text") for a twitter thread. Never raises — an
    unrecognized/empty payload just yields None, displayed as a placeholder
    by the frontend rather than breaking the row.
    """
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    thread = payload.get("thread")
    if isinstance(thread, list) and thread and isinstance(thread[0], dict):
        first_text = thread[0].get("text")
        if isinstance(first_text, str) and first_text.strip():
            return first_text.strip()
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    caption = payload.get("caption")
    if isinstance(caption, str) and caption.strip():
        return caption.strip()
    return None


class JobOut(BaseModel):
    id: int
    platform: str
    status: JobStatus
    attempts: int
    error_message: str | None
    scheduled_at: str | None
    created_at: str
    updated_at: str
    client_id: int | None
    client_name: str | None
    caption: str | None

    @staticmethod
    def from_job(job: Job, client_name: str | None = None) -> "JobOut":
        return JobOut(
            id=job.id,
            platform=job.platform,
            status=job.status,
            attempts=job.attempts,
            error_message=job.error_message,
            scheduled_at=job.scheduled_at.isoformat() if job.scheduled_at else None,
            created_at=job.created_at.isoformat(),
            updated_at=job.updated_at.isoformat(),
            client_id=job.client_id,
            client_name=client_name,
            caption=_extract_caption(job.payload),
        )


class StatsOut(BaseModel):
    total: int
    by_status: dict[str, int]


class AnalyticsTimePoint(BaseModel):
    date: str  # YYYY-MM-DD, UTC
    count: int


class AnalyticsPlatformRow(BaseModel):
    platform: str
    total: int
    published: int
    failed: int
    last_activity: str | None  # ISO8601 of the most recent updated_at for the platform


class AnalyticsSummaryOut(BaseModel):
    total: int
    published: int
    failed: int
    success_rate: float  # published / (published + failed); 0.0 when neither exists
    by_status: dict[str, int]
    by_platform: dict[str, int]
    time_series: list[AnalyticsTimePoint]  # one point per day, last 30 days, gaps filled with 0
    platform_breakdown: list[AnalyticsPlatformRow]


class RetryOut(BaseModel):
    id: int
    status: JobStatus


class AccountOut(BaseModel):
    id: int
    platform: str
    name: str
    is_active: bool
    created_at: str
    client_id: int | None
    client_name: str | None
    # Deliberately no credentials field: this response is served to the
    # browser, and Account.credentials holds live OAuth tokens / API
    # secrets (see app/models.py) that must never leave the server.

    @staticmethod
    def from_account(account: Account, client_name: str | None = None) -> "AccountOut":
        return AccountOut(
            id=account.id,
            platform=account.platform,
            name=account.name,
            is_active=account.is_active,
            created_at=account.created_at.isoformat(),
            client_id=account.client_id,
            client_name=client_name,
        )


class JobCreateOut(BaseModel):
    id: int


class ClientOut(BaseModel):
    id: int
    name: str
    kind: str
    created_at: str

    @staticmethod
    def from_client(client: Client) -> "ClientOut":
        return ClientOut(
            id=client.id,
            name=client.name,
            kind=client.kind,
            created_at=client.created_at.isoformat(),
        )


class ClientCreate(BaseModel):
    name: str
    kind: str = "client"


class RegisterIn(BaseModel):
    email: str
    password: str
    client_name: str


class LoginIn(BaseModel):
    email: str
    password: str


class MeOut(BaseModel):
    authenticated: bool
    role: str | None = None
    client_id: int | None = None
    client_name: str | None = None
    email: str | None = None


class PendingUserOut(BaseModel):
    id: int
    email: str
    requested_client_name: str | None
    created_at: str


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    client_id: int | None
    client_name: str | None
    is_approved: bool
    created_at: str


class ApproveUserIn(BaseModel):
    client_id: int


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """
    Liveness probe for Fly's http_check (see fly.toml) — no auth (see
    _NO_AUTH_PATHS above), and always answers 200 even if the DB check
    fails, since a DB outage should surface as a degraded `db` field, not
    take the machine out of rotation/restart it.
    """
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        logger.exception("Health check DB probe failed")
        db_status = "error"
    return {"status": "ok", "db": db_status}


@app.post("/api/auth/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    """
    Self-registration (Phase 28): always creates a role="client_user" row
    with is_approved=False — there is no way to self-register as an admin.
    The requester names their client workspace by free-text name
    (requested_client_name), not client_id, since they have no way to know
    internal ids; an admin resolves that to a real Client at approval time
    (see approve_user below).

    Public route, no auth required. Deliberately returns the exact same
    response whether or not the email was already registered — this is the
    "don't reveal whether an email exists" requirement, applied by making
    the duplicate-email case a silent no-op rather than a distinguishable
    error. See CLAUDE.md Phase 28 for the one deliberate exception to this
    (login's "pending approval" message, which is more specific by design).
    """
    email = body.email.strip().lower()
    password = body.password
    client_name = body.client_name.strip()

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if not client_name:
        raise HTTPException(status_code=400, detail="Tell us which client workspace you're requesting access to.")

    generic_response = {
        "status": "submitted",
        "detail": "If this email isn't already registered, your request has been submitted for admin approval.",
    }

    existing = db.query(User).filter(func.lower(User.email) == email).one_or_none()
    if existing is not None:
        return generic_response

    user = User(
        email=email,
        hashed_password=hash_password(password),
        role="client_user",
        client_id=None,
        requested_client_name=client_name,
        is_approved=False,
    )
    db.add(user)
    db.commit()
    return generic_response


@app.post("/api/auth/login", response_model=MeOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    """
    Public route. Checked only against the User table — the Basic-Auth
    "ops" admin credentials (DASHBOARD_USERNAME/PASSWORD) are a completely
    separate mechanism (see enforce_auth above) and aren't accepted here.

    Security tradeoff, deliberately made (see CLAUDE.md Phase 28): an
    unknown email and a wrong password both raise the exact same generic
    401, so a login attempt can't be used to enumerate registered emails.
    A *pending* account gets a distinctly-worded 403 instead, per the phase
    brief's explicit ask for a clear pending-approval message — this does
    narrowly reveal that the email is registered-but-not-yet-approved,
    accepted as a smaller, intentional exception rather than applying full
    non-enumeration everywhere.
    """
    identifier = body.email.strip().lower()
    user = db.query(User).filter(func.lower(User.email) == identifier).one_or_none()

    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Your account is pending admin approval.")

    token = create_session_token(user.id)
    client_name = None
    if user.client_id is not None:
        client = db.get(Client, user.client_id)
        client_name = client.name if client else None

    response = JSONResponse(
        MeOut(
            authenticated=True,
            role=user.role,
            client_id=user.client_id,
            client_name=client_name,
            email=user.email,
        ).model_dump()
    )
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/logout")
def logout():
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(_SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/api/auth/me", response_model=MeOut)
def auth_me(request: Request = None, db: Session = Depends(get_db)):
    """
    How the SPA silently checks "am I logged in?" on load without ever
    triggering enforce_auth's 401 (this path is in _PUBLIC_API_PATHS) —
    letting it show the Login screen for an anonymous visitor instead of an
    error, and letting an already-authenticated visitor skip straight to
    the app.
    """
    auth = _get_auth(request)
    if auth.role == "anonymous":
        return MeOut(authenticated=False)

    client_name = None
    if auth.client_id is not None:
        client = db.get(Client, auth.client_id)
        client_name = client.name if client else None

    return MeOut(authenticated=True, role=auth.role, client_id=auth.client_id, client_name=client_name, email=auth.email)


@app.get("/api/oauth/youtube/start")
def youtube_oauth_start(request: Request = None):
    """
    Starts the in-browser YouTube connect flow (Phase 29a) — client_user
    self-service onboarding, replacing scripts/authorize_youtube.py's CLI
    flow for client teams who can't run a local script themselves. Not
    offered to the admin path (Basic-Auth or an admin User session): an
    admin isn't scoped to any one Client, and this flow always creates the
    resulting Account under the caller's own client_id, encoded (signed)
    into the OAuth "state" param below since Google's callback redirect
    carries no session cookie to read it from otherwise.

    Google's Flow object auto-generates PKCE by default, so the
    authorization request always carries a code_challenge — the verifier
    has to survive the round trip to be usable at exchange time. Like
    Twitter's/TikTok's flows, the verifier rides inside the signed
    oauth-state token alongside client_id/user_id (see
    app/publishers/youtube.py's module docstring "PKCE" note for why this
    was previously missing and broke the exchange).
    """
    auth = _get_auth(request)
    if auth.role != "client_user":
        raise HTTPException(status_code=403, detail="YouTube self-service connect is only available to a client account.")

    redirect_uri = str(request.url_for("youtube_oauth_callback"))
    code_verifier, code_challenge = _generate_pkce_pair()
    state = create_oauth_state_token({"client_id": auth.client_id, "user_id": auth.user_id, "code_verifier": code_verifier})
    try:
        authorization_url = youtube_publisher.build_authorization_url(redirect_uri, state, code_challenge)
    except PermanentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(authorization_url)


@app.get("/api/oauth/youtube/callback")
def youtube_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Public callback Google redirects the browser back to after the consent
    screen from youtube_oauth_start above (see _PUBLIC_API_PATHS — Google's
    redirect carries no session cookie, so this route's only trust anchor is
    the signed "state" param, not request.state.auth). Verifies state,
    exchanges the code for credentials, and upserts an Account row
    (platform="youtube") scoped to the client_id encoded in state, via the
    same insert-or-update-by-platform+name helper
    scripts/authorize_youtube.py --account uses — reconnecting the same
    channel rotates its stored token in place instead of creating a
    duplicate Account.

    Always redirects back into the SPA (never a raw JSON error): this is a
    full-page browser navigation, not a fetch() call, so errors are reported
    via a query string the frontend reads and displays rather than an HTTP
    error status the browser would render as a bare error page.

    The PKCE code_verifier generated in youtube_oauth_start is pulled back
    out of state (it never reaches Google's callback redirect on its own)
    and passed to exchange_code_for_credentials, which Google uses to
    verify this exchange belongs to the same flow that sent the
    code_challenge.
    """
    if error:
        return RedirectResponse(f"/?screen=accounts&youtube_connect=error&reason={quote(error)}")
    if not code or not state:
        return RedirectResponse("/?screen=accounts&youtube_connect=error&reason=missing_code_or_state")

    state_data = verify_oauth_state_token(state)
    if state_data is None:
        return RedirectResponse("/?screen=accounts&youtube_connect=error&reason=invalid_or_expired_state")

    code_verifier = state_data.get("code_verifier")
    if not code_verifier:
        return RedirectResponse("/?screen=accounts&youtube_connect=error&reason=missing_pkce_verifier")

    client_id = state_data.get("client_id")
    client = db.get(Client, client_id) if client_id is not None else None
    if client is None:
        return RedirectResponse("/?screen=accounts&youtube_connect=error&reason=unknown_client")

    redirect_uri = str(request.url_for("youtube_oauth_callback"))
    try:
        credentials = youtube_publisher.exchange_code_for_credentials(code, redirect_uri, code_verifier)
    except PermanentError as exc:
        logger.warning("YouTube OAuth code exchange failed for client %s: %s", client_id, exc)
        return RedirectResponse("/?screen=accounts&youtube_connect=error&reason=exchange_failed")

    account, _action = upsert_account(db, "youtube", f"{client.name} (self-service)", credentials)
    account.client_id = client.id
    db.commit()

    return RedirectResponse("/?screen=accounts&youtube_connect=success")


def _generate_pkce_pair() -> tuple[str, str]:
    """
    Standard RFC 7636 PKCE pair: code_challenge = BASE64URL(SHA256(verifier)),
    no padding. Originally added for X's OAuth 2.0 authorize flow (Phase
    29b); also used by youtube_oauth_start since Google's Flow object
    requires PKCE too (see app/publishers/youtube.py's module docstring
    "PKCE" note). Unlike scripts/authorize_tiktok.py's _generate_pkce_pair
    (TikTok's deliberate hex-digest deviation), both X and Google follow the
    RFC exactly — do not copy that trick here.
    """
    verifier = secrets.token_urlsafe(64)  # ~86 chars, within RFC 7636's 43-128 range
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _use_loopback_ip_for_local_host(redirect_uri: str) -> str:
    """
    X's OAuth 2.0 authorize endpoint rejects "localhost" redirect_uris
    outright (a generic "Something went wrong" error) — for non-HTTPS local
    development it only accepts the loopback IP 127.0.0.1 instead (confirmed
    via X's own developer forum, not documented in its official OAuth
    docs). This only rewrites the redirect_uri sent to X as part of the
    authorize request (twitter_oauth_start, below) — the callback ROUTE
    itself is unaffected (FastAPI matches by path, not host): once X
    redirects the browser back to this rewritten URI, request.url_for in
    twitter_oauth_callback naturally reproduces "127.0.0.1" too, since
    that's the real Host header on that follow-up request.
    """
    parts = urlsplit(redirect_uri)
    if parts.hostname != "localhost":
        return redirect_uri
    netloc = "127.0.0.1" if parts.port is None else f"127.0.0.1:{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


@app.get("/api/oauth/twitter/start")
def twitter_oauth_start(request: Request = None):
    """
    Starts the in-browser X/Twitter connect flow (Phase 29b), mirroring
    youtube_oauth_start (Phase 29a) exactly — client_user-only, since this
    flow always creates the resulting Account under the caller's own
    client_id.

    X's OAuth 2.0 requires PKCE even for confidential clients (see
    app/publishers/twitter.py::build_authorization_url), so unlike
    YouTube's state (just client_id/user_id) this one also carries the PKCE
    code_verifier — the callback has no other way to recover it, since X's
    redirect back never echoes it. It rides inside the same signed
    oauth-state token as client_id/user_id, so it's tamper-evident and
    short-lived (10 minutes) exactly like the rest of the state.

    redirect_uri is rewritten to use 127.0.0.1 instead of localhost (see
    _use_loopback_ip_for_local_host above) before being sent to X — X's
    authorize endpoint rejects "localhost" outright for local dev.
    """
    auth = _get_auth(request)
    if auth.role != "client_user":
        raise HTTPException(status_code=403, detail="X/Twitter self-service connect is only available to a client account.")

    redirect_uri = _use_loopback_ip_for_local_host(str(request.url_for("twitter_oauth_callback")))
    code_verifier, code_challenge = _generate_pkce_pair()
    state = create_oauth_state_token({"client_id": auth.client_id, "user_id": auth.user_id, "code_verifier": code_verifier})
    try:
        authorization_url = twitter_publisher.build_authorization_url(redirect_uri, state, code_challenge)
    except PermanentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(authorization_url)


@app.get("/api/oauth/twitter/callback")
def twitter_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Public callback X redirects the browser back to after the consent
    screen from twitter_oauth_start above — mirrors youtube_oauth_callback
    (Phase 29a) exactly, see its docstring for why this must stay public
    and redirect-only. The one addition: the PKCE code_verifier is pulled
    back out of state (see twitter_oauth_start) and passed to
    exchange_code_for_credentials, which X uses to verify this exchange
    belongs to the same flow that sent the code_challenge.
    """
    if error:
        return RedirectResponse(f"/?screen=accounts&twitter_connect=error&reason={quote(error)}")
    if not code or not state:
        return RedirectResponse("/?screen=accounts&twitter_connect=error&reason=missing_code_or_state")

    state_data = verify_oauth_state_token(state)
    if state_data is None:
        return RedirectResponse("/?screen=accounts&twitter_connect=error&reason=invalid_or_expired_state")

    code_verifier = state_data.get("code_verifier")
    if not code_verifier:
        return RedirectResponse("/?screen=accounts&twitter_connect=error&reason=missing_pkce_verifier")

    client_id = state_data.get("client_id")
    client = db.get(Client, client_id) if client_id is not None else None
    if client is None:
        return RedirectResponse("/?screen=accounts&twitter_connect=error&reason=unknown_client")

    redirect_uri = str(request.url_for("twitter_oauth_callback"))
    try:
        credentials = twitter_publisher.exchange_code_for_credentials(code, redirect_uri, code_verifier)
    except PermanentError as exc:
        logger.warning("X OAuth code exchange failed for client %s: %s", client_id, exc)
        return RedirectResponse("/?screen=accounts&twitter_connect=error&reason=exchange_failed")

    account, _action = upsert_account(db, "twitter", f"{client.name} (self-service)", credentials)
    account.client_id = client.id
    db.commit()

    return RedirectResponse("/?screen=accounts&twitter_connect=success")


def _generate_tiktok_pkce_pair() -> tuple[str, str]:
    """
    TikTok's non-standard PKCE pair (Phase 29c) — the challenge is the raw
    HEX digest of SHA256(verifier), NOT standard RFC 7636 base64url (see
    _generate_pkce_pair above for X's standard version, and
    scripts/authorize_tiktok.py::_generate_pkce_pair, whose exact shape this
    mirrors, for why TikTok's Login Kit requires the deviation). Do not
    "fix" this to match _generate_pkce_pair — that would break against
    TikTok's login page.
    """
    verifier = secrets.token_urlsafe(64)  # ~86 chars, within RFC 7636's 43-128 range
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    return verifier, challenge


@app.get("/api/oauth/tiktok/start")
def tiktok_oauth_start(request: Request = None):
    """
    Starts the in-browser TikTok connect flow (Phase 29c), mirroring
    youtube_oauth_start (29a) / twitter_oauth_start (29b) — client_user-only,
    since this flow always creates the resulting Account under the caller's
    own client_id. Like Twitter's, this needs PKCE, but with TikTok's
    non-standard hex-digest challenge (_generate_tiktok_pkce_pair) rather
    than X's standard RFC 7636 one — the verifier rides inside the signed
    state token exactly the same way Twitter's does, since TikTok's callback
    never echoes it back either.
    """
    auth = _get_auth(request)
    if auth.role != "client_user":
        raise HTTPException(status_code=403, detail="TikTok self-service connect is only available to a client account.")

    redirect_uri = str(request.url_for("tiktok_oauth_callback"))
    code_verifier, code_challenge = _generate_tiktok_pkce_pair()
    state = create_oauth_state_token({"client_id": auth.client_id, "user_id": auth.user_id, "code_verifier": code_verifier})
    try:
        authorization_url = tiktok_publisher.build_authorization_url(redirect_uri, state, code_challenge)
    except PermanentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(authorization_url)


@app.get("/api/oauth/tiktok/callback")
def tiktok_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Public callback TikTok redirects the browser back to after the consent
    screen from tiktok_oauth_start above — mirrors
    youtube_oauth_callback/twitter_oauth_callback exactly, see their
    docstrings for why this must stay public and redirect-only. Like
    Twitter's callback, the PKCE code_verifier is pulled back out of state
    and passed to exchange_code_for_credentials, which TikTok uses to verify
    this exchange belongs to the same flow that sent the code_challenge.
    """
    if error:
        return RedirectResponse(f"/?screen=accounts&tiktok_connect=error&reason={quote(error)}")
    if not code or not state:
        return RedirectResponse("/?screen=accounts&tiktok_connect=error&reason=missing_code_or_state")

    state_data = verify_oauth_state_token(state)
    if state_data is None:
        return RedirectResponse("/?screen=accounts&tiktok_connect=error&reason=invalid_or_expired_state")

    code_verifier = state_data.get("code_verifier")
    if not code_verifier:
        return RedirectResponse("/?screen=accounts&tiktok_connect=error&reason=missing_pkce_verifier")

    client_id = state_data.get("client_id")
    client = db.get(Client, client_id) if client_id is not None else None
    if client is None:
        return RedirectResponse("/?screen=accounts&tiktok_connect=error&reason=unknown_client")

    redirect_uri = str(request.url_for("tiktok_oauth_callback"))
    try:
        credentials = tiktok_publisher.exchange_code_for_credentials(code, redirect_uri, code_verifier)
    except PermanentError as exc:
        logger.warning("TikTok OAuth code exchange failed for client %s: %s", client_id, exc)
        return RedirectResponse("/?screen=accounts&tiktok_connect=error&reason=exchange_failed")

    account, _action = upsert_account(db, "tiktok", f"{client.name} (self-service)", credentials)
    account.client_id = client.id
    db.commit()

    return RedirectResponse("/?screen=accounts&tiktok_connect=success")


@app.get("/api/oauth/meta/start")
def meta_oauth_start(request: Request = None):
    """
    Starts the in-browser Facebook + Instagram connect flow (Phase 29d),
    mirroring youtube_oauth_start/twitter_oauth_start/tiktok_oauth_start —
    client_user-only, since this flow always creates the resulting
    Account(s) under the caller's own client_id.

    Unlike Google/X/TikTok, Meta's OAuth dialog needs no PKCE (see
    app/publishers/meta.py::build_authorization_url), so state only carries
    client_id/user_id, same shape as YouTube's.

    One click covers both platforms per Meta's actual OAuth model: a single
    consent grant yields a user token that lists every Page the user
    manages (and, per Page, its linked Instagram Business account, if any)
    — see meta_oauth_callback below, which connects all of them rather than
    prompting for a single choice the way scripts/authorize_meta.py's CLI
    flow does (a client_user is expected to manage their own Page(s) only,
    so no picker is needed for self-service).
    """
    auth = _get_auth(request)
    if auth.role != "client_user":
        raise HTTPException(
            status_code=403, detail="Facebook/Instagram self-service connect is only available to a client account."
        )

    redirect_uri = str(request.url_for("meta_oauth_callback"))
    state = create_oauth_state_token({"client_id": auth.client_id, "user_id": auth.user_id})
    try:
        authorization_url = meta_publisher.build_authorization_url(redirect_uri, state)
    except PermanentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(authorization_url)


@app.get("/api/oauth/meta/callback")
def meta_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Public callback Meta redirects the browser back to after the consent
    dialog from meta_oauth_start above — mirrors youtube_oauth_callback's
    shape (see its docstring for why this must stay public and
    redirect-only), but reuses app/publishers/meta.py's existing OAuth-chain
    functions directly (exchange_code_for_user_token ->
    exchange_long_lived_token -> list_pages -> get_instagram_business_account
    per Page) rather than a single exchange_code_for_credentials wrapper,
    since one authorization can yield more than one Account.

    Connects every Page the user manages (no picker, unlike
    scripts/authorize_meta.py's interactive _choose_page — see
    meta_oauth_start's docstring for why that's fine for self-service): each
    Page upserts a facebook Account, and — only if that Page has a linked
    Instagram Business account — an instagram Account too. Account names are
    "<Client name> - <Page name> (self-service)" rather than just
    "<Client name> (self-service)" (YouTube's/Twitter's/TikTok's shape),
    since a client can have more than one Page and upsert_account matches on
    platform+name — reconnecting the same Page for the same client rotates
    that Page's Account(s) in place; a genuinely new Page gets new ones.

    Any PublishError (Transient or Permanent) anywhere in the exchange chain
    redirects with reason=exchange_failed and leaves no Account behind (a
    single db.commit() at the end, not per-Page) — same one-shot,
    human-driven-retry contract as the other three platforms' callbacks,
    even though meta.py's chain functions (unlike youtube.py's/twitter.py's/
    tiktok.py's exchange_code_for_credentials) don't themselves normalize
    Transient/Permanent, since nothing here needs to distinguish them for
    the Beat-task's benefit the way refresh_stored_credentials does.
    """
    if error:
        return RedirectResponse(f"/?screen=accounts&meta_connect=error&reason={quote(error)}")
    if not code or not state:
        return RedirectResponse("/?screen=accounts&meta_connect=error&reason=missing_code_or_state")

    state_data = verify_oauth_state_token(state)
    if state_data is None:
        return RedirectResponse("/?screen=accounts&meta_connect=error&reason=invalid_or_expired_state")

    client_id = state_data.get("client_id")
    client = db.get(Client, client_id) if client_id is not None else None
    if client is None:
        return RedirectResponse("/?screen=accounts&meta_connect=error&reason=unknown_client")

    redirect_uri = str(request.url_for("meta_oauth_callback"))
    try:
        short_lived = meta_publisher.exchange_code_for_user_token(code, redirect_uri)
        long_lived = meta_publisher.exchange_long_lived_token(short_lived["access_token"])
        user_token = long_lived["access_token"]
        user_token_expires_at = long_lived["expires_at"]
        pages = meta_publisher.list_pages(user_token)

        facebook_count = 0
        instagram_count = 0
        for page in pages:
            page_id = page.get("id")
            page_token = page.get("access_token")
            if not page_id or not page_token:
                continue
            page_name = page.get("name", "")
            account_name = f"{client.name} - {page_name or page_id} (self-service)"

            facebook_credentials = {
                "page_id": page_id,
                "page_token": page_token,
                "page_name": page_name,
                "user_token": user_token,
                "user_token_expires_at": user_token_expires_at,
            }
            fb_account, _action = upsert_account(db, "facebook", account_name, facebook_credentials)
            fb_account.client_id = client.id
            facebook_count += 1

            ig_user_id = meta_publisher.get_instagram_business_account(page_id, page_token)
            if ig_user_id:
                instagram_credentials = {
                    "ig_user_id": ig_user_id,
                    "page_id": page_id,
                    "page_token": page_token,
                    "user_token": user_token,
                    "user_token_expires_at": user_token_expires_at,
                }
                ig_account, _action = upsert_account(db, "instagram", account_name, instagram_credentials)
                ig_account.client_id = client.id
                instagram_count += 1
    except PublishError as exc:
        db.rollback()
        logger.warning("Meta OAuth code exchange failed for client %s: %s", client_id, exc)
        return RedirectResponse("/?screen=accounts&meta_connect=error&reason=exchange_failed")

    if facebook_count == 0:
        db.rollback()
        return RedirectResponse("/?screen=accounts&meta_connect=error&reason=no_pages")

    db.commit()
    return RedirectResponse(f"/?screen=accounts&meta_connect=success&facebook={facebook_count}&instagram={instagram_count}")


@app.get("/api/admin/users/pending", response_model=list[PendingUserOut])
def list_pending_users(request: Request = None, db: Session = Depends(get_db)):
    _require_admin(request)
    users = db.query(User).filter(User.is_approved.is_(False)).order_by(User.created_at).all()
    return [
        PendingUserOut(
            id=u.id,
            email=u.email,
            requested_client_name=u.requested_client_name,
            created_at=u.created_at.isoformat(),
        )
        for u in users
    ]


@app.get("/api/admin/users", response_model=list[UserOut])
def list_approved_users(request: Request = None, db: Session = Depends(get_db)):
    """Read-only for now — editing role/client or deactivating a user isn't built this phase."""
    _require_admin(request)
    users = db.query(User).filter(User.is_approved.is_(True)).order_by(User.created_at).all()
    client_names = {c.id: c.name for c in db.query(Client.id, Client.name)}
    return [
        UserOut(
            id=u.id,
            email=u.email,
            role=u.role,
            client_id=u.client_id,
            client_name=client_names.get(u.client_id),
            is_approved=u.is_approved,
            created_at=u.created_at.isoformat(),
        )
        for u in users
    ]


@app.post("/api/admin/users/{user_id}/approve", response_model=UserOut)
def approve_user(user_id: int, body: ApproveUserIn, request: Request = None, db: Session = Depends(get_db)):
    """Assigns/confirms the Client (the admin picks it, using requested_client_name only as a hint) and flips is_approved."""
    _require_admin(request)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    client = db.get(Client, body.client_id)
    if client is None:
        raise HTTPException(status_code=404, detail=f"Client {body.client_id} not found")

    user.client_id = client.id
    user.is_approved = True
    db.commit()
    db.refresh(user)

    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        client_id=user.client_id,
        client_name=client.name,
        is_approved=user.is_approved,
        created_at=user.created_at.isoformat(),
    )


@app.post("/api/admin/users/{user_id}/reject", status_code=204)
def reject_user(user_id: int, request: Request = None, db: Session = Depends(get_db)):
    """
    Deletes a still-pending registration request. Only for pending users —
    rejecting/deactivating an already-approved user isn't built this phase
    (see CLAUDE.md Phase 28), so that case gets a 400 instead of silently
    deleting a live account.
    """
    _require_admin(request)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    if user.is_approved:
        raise HTTPException(
            status_code=400,
            detail="Cannot reject an already-approved user (deactivation isn't implemented yet).",
        )
    db.delete(user)
    db.commit()


@app.get("/api/jobs", response_model=list[JobOut])
def list_jobs(
    status: JobStatus | None = None,
    platform: str | None = None,
    client_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    request: Request = None,
    db: Session = Depends(get_db),
):
    auth = _get_auth(request)
    if auth.role == "client_user":
        # Automatic scoping, not just a UI filter: a client_id explicitly
        # requesting another client's data is a 403, and an omitted one is
        # silently forced to the caller's own — this endpoint can never
        # return another client's jobs no matter what's passed.
        if client_id is not None and client_id != auth.client_id:
            raise HTTPException(status_code=403, detail="Cannot access another client's jobs.")
        client_id = auth.client_id

    query = db.query(Job)
    if status is not None:
        query = query.filter(Job.status == status)
    if platform is not None:
        query = query.filter(Job.platform == platform)
    if client_id is not None:
        query = query.filter(Job.client_id == client_id)
    jobs = query.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit).all()
    client_names = {c.id: c.name for c in db.query(Client.id, Client.name)}
    return [JobOut.from_job(job, client_names.get(job.client_id)) for job in jobs]


@app.get("/api/stats", response_model=StatsOut)
def stats(client_id: int | None = None, request: Request = None, db: Session = Depends(get_db)):
    auth = _get_auth(request)
    if auth.role == "client_user":
        if client_id is not None and client_id != auth.client_id:
            raise HTTPException(status_code=403, detail="Cannot access another client's stats.")
        client_id = auth.client_id

    query = db.query(Job.status, func.count(Job.id))
    if client_id is not None:
        query = query.filter(Job.client_id == client_id)
    counts = dict(query.group_by(Job.status).all())
    by_status = {s.value: counts.get(s, 0) for s in JobStatus}
    return StatsOut(total=sum(by_status.values()), by_status=by_status)


# Width of the /api/analytics/summary time series, in days (inclusive of today).
_ANALYTICS_TIME_SERIES_DAYS = 30


def _isoformat_or_none(value) -> str | None:
    """
    func.max(Job.updated_at) comes back as a datetime under Postgres but can
    surface as a raw ISO string under SQLite (the test DB) depending on how
    SQLAlchemy carries the DateTime type through the aggregate — accept both.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


@app.get("/api/analytics/summary", response_model=AnalyticsSummaryOut)
def analytics_summary(client_id: int | None = None, request: Request = None, db: Session = Depends(get_db)):
    """
    Aggregate job metrics for the scheduler UI's Analytics screen (Phase 30).
    Everything here is derived from the existing Job table — counts grouped
    by platform and by status, a 30-day daily time series of jobs created,
    and a per-platform published/failed/last-activity breakdown. No schema
    changes, no new columns.

    Scoped exactly like GET /api/stats: a client_user is silently confined
    to their own client_id, and an explicit different client_id is a 403 —
    this can never surface another client's numbers.
    """
    auth = _get_auth(request)
    if auth.role == "client_user":
        if client_id is not None and client_id != auth.client_id:
            raise HTTPException(status_code=403, detail="Cannot access another client's analytics.")
        client_id = auth.client_id

    def _scoped(query):
        return query.filter(Job.client_id == client_id) if client_id is not None else query

    # Counts by status.
    status_counts = dict(_scoped(db.query(Job.status, func.count(Job.id))).group_by(Job.status).all())
    by_status = {s.value: status_counts.get(s, 0) for s in JobStatus}
    total = sum(by_status.values())
    published = by_status[JobStatus.PUBLISHED.value]
    failed = by_status[JobStatus.FAILED.value]
    denom = published + failed
    success_rate = round(published / denom, 4) if denom else 0.0

    # Counts by (platform, status) -> feeds both by_platform and the breakdown.
    platform_status_rows = (
        _scoped(db.query(Job.platform, Job.status, func.count(Job.id)))
        .group_by(Job.platform, Job.status)
        .all()
    )
    by_platform: dict[str, int] = {}
    per_platform: dict[str, dict[str, int]] = {}
    for platform, status, count in platform_status_rows:
        by_platform[platform] = by_platform.get(platform, 0) + count
        bucket = per_platform.setdefault(platform, {"published": 0, "failed": 0})
        if status == JobStatus.PUBLISHED:
            bucket["published"] += count
        elif status == JobStatus.FAILED:
            bucket["failed"] += count

    last_activity = dict(
        _scoped(db.query(Job.platform, func.max(Job.updated_at))).group_by(Job.platform).all()
    )

    platform_breakdown = [
        AnalyticsPlatformRow(
            platform=platform,
            total=by_platform[platform],
            published=per_platform.get(platform, {}).get("published", 0),
            failed=per_platform.get(platform, {}).get("failed", 0),
            last_activity=_isoformat_or_none(last_activity.get(platform)),
        )
        for platform in sorted(by_platform)
    ]

    # Daily time series: jobs created per UTC day, last _ANALYTICS_TIME_SERIES_DAYS
    # days, with every day present (zero-filled) so the frontend chart has a
    # fixed-width x-axis regardless of gaps.
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=_ANALYTICS_TIME_SERIES_DAYS - 1)
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    buckets = {
        (start_date + timedelta(days=i)).isoformat(): 0
        for i in range(_ANALYTICS_TIME_SERIES_DAYS)
    }
    created_rows = _scoped(db.query(Job.created_at)).filter(Job.created_at >= start_dt).all()
    for (created,) in created_rows:
        if created is None:
            continue
        key = created.date().isoformat()
        if key in buckets:
            buckets[key] += 1
    time_series = [AnalyticsTimePoint(date=key, count=buckets[key]) for key in sorted(buckets)]

    return AnalyticsSummaryOut(
        total=total,
        published=published,
        failed=failed,
        success_rate=success_rate,
        by_status=by_status,
        by_platform=by_platform,
        time_series=time_series,
        platform_breakdown=platform_breakdown,
    )


@app.post("/api/jobs/{job_id}/retry", response_model=RetryOut)
def retry_job(job_id: int, request: Request = None, db: Session = Depends(get_db)):
    auth = _get_auth(request)
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if auth.role == "client_user" and job.client_id != auth.client_id:
        raise HTTPException(status_code=403, detail="Cannot retry another client's job.")
    if job.status != JobStatus.FAILED:
        raise HTTPException(
            status_code=409,
            detail=f"Job {job_id} is {job.status.value}, not failed; only failed jobs can be retried",
        )

    job.status = JobStatus.QUEUED
    job.attempts = 0
    job.error_message = None
    db.commit()

    publish_job.delay(job_id)

    return RetryOut(id=job.id, status=job.status)


@app.get("/api/accounts", response_model=list[AccountOut])
def list_accounts(
    platform: str | None = None,
    client_id: int | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
):
    auth = _get_auth(request)
    if auth.role == "client_user":
        if client_id is not None and client_id != auth.client_id:
            raise HTTPException(status_code=403, detail="Cannot access another client's accounts.")
        client_id = auth.client_id

    query = db.query(Account)
    if platform is not None:
        query = query.filter(Account.platform == platform)
    if client_id is not None:
        query = query.filter(Account.client_id == client_id)
    accounts = query.order_by(Account.platform, Account.name).all()
    client_names = {c.id: c.name for c in db.query(Client.id, Client.name)}
    return [AccountOut.from_account(account, client_names.get(account.client_id)) for account in accounts]


@app.get("/api/clients", response_model=list[ClientOut])
def list_clients(request: Request = None, db: Session = Depends(get_db)):
    """
    Admin-only (Phase 28): the full client roster (names of every agency
    client) isn't something a client_user should be able to enumerate, even
    though it's not explicitly called out in the phase brief — the same
    "must never see another client's data" principle extends naturally to
    the client list itself.
    """
    _require_admin(request)
    clients = db.query(Client).order_by(Client.name).all()
    return [ClientOut.from_client(client) for client in clients]


@app.post("/api/clients", response_model=ClientOut, status_code=201)
def create_client(body: ClientCreate, request: Request = None, db: Session = Depends(get_db)):
    """
    Creates a Client workspace (Phase 26 — see app/models.py::Client). Bare
    minimum for the scheduler UI's client switcher/"Add client workspace"
    tile: no dedup-by-name, no update/delete route yet — narrow enough that
    adding those later is additive, not a breaking change. Admin-only
    (Phase 28) — a client_user has no reason to create workspaces.
    """
    _require_admin(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    client = Client(name=name, kind=body.kind.strip() or "client")
    db.add(client)
    db.commit()
    db.refresh(client)
    return ClientOut.from_client(client)


async def _save_upload(file: UploadFile) -> Path:
    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename).name
    dest_path = _UPLOADS_DIR / f"{uuid.uuid4().hex}_{safe_name}"
    dest_path.write_bytes(await file.read())
    return dest_path


def _stage_to_r2(local_path: Path) -> str | None:
    """
    Best-effort side upload of a locally-saved job file to R2 (Phase 22):
    the local path stays every publisher's source of truth today, this
    just also makes a public_url available for the upcoming Instagram
    publisher (which needs a public URL, not a local path) and as
    groundwork for the Fly deploy. Never raises — R2 being unconfigured or
    unreachable must never break job creation, so a missing/failed upload
    just means no public_url gets attached, same as before this phase.
    """
    try:
        return storage.upload_file(str(local_path))["public_url"]
    except StorageNotConfiguredError:
        return None
    except Exception:
        logger.exception("R2 upload failed for %s; continuing without a public_url", local_path)
        return None


@app.post("/api/jobs", response_model=JobCreateOut, status_code=201)
async def create_job(
    platform: str = Form(...),
    file: UploadFile | None = File(default=None),
    media_files: list[UploadFile] = File(default=[]),
    account_id: int | None = Form(default=None),
    client_id: int | None = Form(default=None),
    title: str | None = Form(default=None),
    text: str | None = Form(default=None),
    privacy: str | None = Form(default=None),
    shorts: bool = Form(default=False),
    playlist_id: str | None = Form(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Job creation for the dashboard's "New Job" tab. Builds the same payload
    shape the corresponding scripts/enqueue_*_test.py script does, then
    dispatches exactly like they do (publish_job.delay) — this route is a
    thin HTTP front end over that same pattern, not a new way of
    constructing jobs.

    Three payload shapes, by platform (_VIDEO_UPLOAD_PLATFORMS above):
      - youtube/tiktok: a single required video `file`. privacy/shorts/
        playlist_id (Phase 15) are youtube-only and passed straight through
        into the payload — app/publishers/youtube.py owns all the actual
        validation (Shorts duration/aspect-ratio, playlist assignment).
      - twitter (Phase 21): a required `text` field plus optional
        `media_files` (0 or more) — app/publishers/twitter.py owns the
        4-images-or-1-video-never-mixed cap and file-type validation, same
        "publisher owns validation, this route doesn't duplicate it" split
        as every other platform here. No file is required; threads aren't
        exposed through this form (script-only, see
        scripts/enqueue_twitter_test.py --mode thread).
      - facebook (Phase 24): `text` and/or a single `media_files` entry —
        unlike twitter, neither is independently required, only "at least
        one of the two" (a media-only post with no caption is valid).
        app/publishers/facebook.py auto-detects photo vs. video from the
        file's type and owns all further validation; this route only
        enforces the "at most one" cap up front so a bad multi-file upload
        fails fast instead of saving files nobody will use.
      - instagram (Phase 25): a single required `media_files` entry (an
        optional `text` caption) — unlike facebook, Instagram has NO
        text-only post type, so media is mandatory here, not optional.
        app/publishers/instagram.py never reads a local file at all: it
        needs a publicly-fetchable URL (Meta downloads the media at publish
        time), so this route requires the R2 staging step below to
        succeed and returns a 400 up front if it doesn't — unlike every
        other platform, where a failed/unconfigured R2 upload silently
        degrades to local-path-only, an instagram job with no public_url
        can never actually publish, so failing fast here beats creating a
        job that's guaranteed to dead-letter in the worker.

    Phase 22: every uploaded file is also best-effort staged to R2
    (_stage_to_r2) — the local path in the payload is unchanged and stays
    what every publisher actually reads today (except instagram, Phase 25,
    which reads ONLY the public_url — see above), but when staging succeeds
    a public "media_public_url" (or "media_public_urls" for twitter's
    per-tweet list) is attached alongside it, for the Instagram publisher
    and as groundwork for the Fly deploy. If R2 isn't configured or the
    upload fails, every platform but instagram still creates the job
    exactly as before this phase — see _stage_to_r2.
    """
    auth = _get_auth(request)
    if auth.role == "client_user":
        # Forced, not merely defaulted: a client_user explicitly naming a
        # different client_id is rejected rather than silently overridden,
        # so a buggy/malicious client can't be quietly redirected either.
        if client_id is not None and client_id != auth.client_id:
            raise HTTPException(status_code=403, detail="Cannot create a job for another client.")
        client_id = auth.client_id

    if platform not in _SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported platform {platform!r}; must be one of {sorted(_SUPPORTED_PLATFORMS)}",
        )

    uploaded_media_files = [mf for mf in media_files if mf.filename]

    if platform in _VIDEO_UPLOAD_PLATFORMS:
        if file is None or not file.filename:
            raise HTTPException(status_code=400, detail="No file uploaded")
    elif platform == "twitter":
        if not text or not text.strip():
            raise HTTPException(status_code=400, detail="Missing required field: text")
    elif platform == "facebook":
        if not (text and text.strip()) and not uploaded_media_files:
            raise HTTPException(status_code=400, detail="Facebook posts need 'text' and/or a media file")
        if len(uploaded_media_files) > 1:
            raise HTTPException(status_code=400, detail="Facebook posts support at most one media file")
    else:  # instagram
        if not uploaded_media_files:
            raise HTTPException(status_code=400, detail="Instagram posts require a media file (no text-only posts)")
        if len(uploaded_media_files) > 1:
            raise HTTPException(status_code=400, detail="Instagram posts support at most one media file")

    if client_id is not None and db.get(Client, client_id) is None:
        raise HTTPException(status_code=404, detail=f"Client {client_id} not found")

    account = None
    if account_id is not None:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
        if account.platform != platform:
            raise HTTPException(
                status_code=400,
                detail=f"Account {account_id} is a {account.platform} account, not {platform}",
            )
        if not account.is_active:
            raise HTTPException(status_code=400, detail=f"Account {account_id} is inactive")
        if auth.role == "client_user" and account.client_id != auth.client_id:
            # Without this check a client_user could post content through
            # another client's connected social account (not just read its
            # data) by guessing/enumerating account_id — a materially worse
            # leak than a read-only cross-tenant view.
            raise HTTPException(status_code=403, detail="Cannot use another client's connected account.")
    elif platform in _ACCOUNT_REQUIRED_PLATFORMS:
        # Same rule the publisher itself enforces (app/publishers/tiktok.py,
        # app/publishers/facebook.py): no single-account/env-var fallback —
        # give a friendlier 400 here instead of letting the job get created
        # and fail later in the worker.
        raise HTTPException(status_code=400, detail=f"{platform} jobs require an account (no single-account fallback)")

    if platform in _VIDEO_UPLOAD_PLATFORMS:
        dest_path = await _save_upload(file)
        public_url = _stage_to_r2(dest_path)
        job_title = title.strip() if title and title.strip() else (
            f"Distribution engine upload {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        )

        if platform == "tiktok":
            payload = {"video_path": str(dest_path), "title": job_title}
        else:
            payload = {
                "video_path": str(dest_path),
                "title": job_title,
                "privacy": privacy or "private",
            }
            if shorts:
                payload["shorts"] = True
            if playlist_id and playlist_id.strip():
                payload["playlist_id"] = playlist_id.strip()
        if public_url:
            payload["media_public_url"] = public_url
    elif platform == "twitter":
        payload = {"text": text.strip()}
        media_paths = [str(await _save_upload(mf)) for mf in uploaded_media_files]
        if media_paths:
            payload["media_paths"] = media_paths
            # Only attached if every file staged successfully — a partial
            # list wouldn't reliably line up with media_paths for a future
            # consumer, and R2 being down must never block job creation.
            public_urls = [_stage_to_r2(Path(p)) for p in media_paths]
            if all(public_urls):
                payload["media_public_urls"] = public_urls
    elif platform == "facebook":
        payload = {}
        if text and text.strip():
            payload["text"] = text.strip()
        if uploaded_media_files:
            dest_path = await _save_upload(uploaded_media_files[0])
            payload["media_paths"] = [str(dest_path)]
            public_url = _stage_to_r2(dest_path)
            if public_url:
                payload["media_public_url"] = public_url
    else:  # instagram
        dest_path = await _save_upload(uploaded_media_files[0])
        public_url = _stage_to_r2(dest_path)
        if not public_url:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Could not stage media to Cloudflare R2 — Instagram requires a publicly-accessible media "
                    "URL (Meta downloads it at publish time). Set R2_ENDPOINT_URL/R2_ACCESS_KEY_ID/"
                    "R2_SECRET_ACCESS_KEY/R2_BUCKET_NAME/R2_PUBLIC_BASE_URL in .env — see .env.example."
                ),
            )
        payload = {"media_public_url": public_url}
        if text and text.strip():
            payload["text"] = text.strip()

    job = Job(
        platform=platform,
        payload=payload,
        account_id=account.id if account else None,
        client_id=client_id,
        status=JobStatus.QUEUED,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    publish_job.delay(job.id)

    return JobCreateOut(id=job.id)


@app.post("/webhooks/tiktok")
async def tiktok_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receives TikTok Content Posting API status callbacks (Phase 10b — see
    CLAUDE.md for the event-naming caveat and app/webhooks/tiktok.py for the
    verification/parsing mechanics). Always responds fast: signature
    verification and envelope parsing are pure/local, the audit-row insert
    is a single fast write, and the actual job-matching + alerting work is
    handed off to handle_tiktok_webhook_event.delay() rather than run inline
    — TikTok expects a prompt 200 and retries with backoff for up to 72h on
    anything else, so nothing slow (e.g. the Discord/Slack alert, an
    outbound HTTP call) can happen in this handler.

    Reads the raw body (not a parsed model) because signature verification
    has to hash the exact bytes TikTok sent — re-serializing a parsed JSON
    object is not guaranteed to reproduce them.
    """
    raw_body = await request.body()

    if not tiktok_webhooks.verification_skipped():
        try:
            tiktok_webhooks.verify_signature(raw_body, request.headers.get(tiktok_webhooks.SIGNATURE_HEADER))
        except tiktok_webhooks.WebhookVerificationError as exc:
            # Unlike an unresolvable publish_id, a bad signature is a
            # security-relevant rejection, not a "we understood you but
            # can't act" case — it must not be masked as a 200. If this is
            # a genuine TikTok request wrongly rejected (e.g. clock skew),
            # TikTok's own retry-with-backoff (up to 72h) gives it another
            # chance once the underlying issue is fixed.
            logger.warning("Rejected TikTok webhook: %s", exc)
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        parsed = tiktok_webhooks.parse_envelope(raw_body)
    except tiktok_webhooks.WebhookPayloadError as exc:
        # Malformed body: nothing about retrying would fix this, so answer
        # 200 rather than triggering TikTok's retry loop over a request
        # we'll never be able to parse.
        logger.warning("Ignoring malformed TikTok webhook body: %s", exc)
        return {"status": "ignored", "reason": str(exc)}

    event = WebhookEvent(
        platform="tiktok",
        event_type=parsed["event_type"],
        publish_id=parsed["publish_id"],
        raw_payload=parsed["raw_envelope"],
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    handle_tiktok_webhook_event.delay(event.id)

    return {"status": "received", "webhook_event_id": event.id}


# Mounted last so it never shadows the /api/* routes above. html=True serves
# index.html for "/" (and for unmatched paths), so this stays a single-page app.
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
