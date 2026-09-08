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

import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import storage
from app.auth import create_session_token, hash_password, verify_password, verify_session_token
from app.db import SessionLocal
from app.exceptions import StorageNotConfiguredError
from app.models import Account, Client, Job, JobStatus, User, WebhookEvent
from app.tasks import handle_tiktok_webhook_event, publish_job
from app.webhooks import tiktok as tiktok_webhooks

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
_PUBLIC_API_PATHS = {"/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/auth/me"}


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
