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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Account, Job, JobStatus, WebhookEvent
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
# one above.
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

# Path exempted from Basic auth: TikTok's servers POST here directly and
# can't supply dashboard credentials — the request's own signature
# (TikTok-Signature header, verified in tiktok_webhook below) is its auth.
_WEBHOOK_PATH = "/webhooks/tiktok"

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


# A single HTTP middleware (rather than a per-route Depends) so this covers
# every route uniformly — API routes, the StaticFiles mount, and FastAPI's
# auto-generated /docs, /redoc, /openapi.json — without having to remember
# to wire it into each one individually. Registered before CORSMiddleware
# below so CORS ends up as the outer layer and keeps handling preflight
# OPTIONS requests (which never carry credentials) without hitting auth.
@app.middleware("http")
async def enforce_basic_auth(request: Request, call_next):
    if not _AUTH_ENABLED or request.url.path == _WEBHOOK_PATH:
        return await call_next(request)

    credentials = await _basic_auth(request)
    if not _credentials_valid(credentials):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing credentials"},
            headers={"WWW-Authenticate": "Basic"},
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
# payload for. Other platforms (fake, twitter, ...) don't take file uploads
# through this flow.
_SUPPORTED_UPLOAD_PLATFORMS = {"youtube", "tiktok"}


class JobOut(BaseModel):
    id: int
    platform: str
    status: JobStatus
    attempts: int
    error_message: str | None
    scheduled_at: str | None
    created_at: str
    updated_at: str

    @staticmethod
    def from_job(job: Job) -> "JobOut":
        return JobOut(
            id=job.id,
            platform=job.platform,
            status=job.status,
            attempts=job.attempts,
            error_message=job.error_message,
            scheduled_at=job.scheduled_at.isoformat() if job.scheduled_at else None,
            created_at=job.created_at.isoformat(),
            updated_at=job.updated_at.isoformat(),
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
    # Deliberately no credentials field: this response is served to the
    # browser, and Account.credentials holds live OAuth tokens / API
    # secrets (see app/models.py) that must never leave the server.

    @staticmethod
    def from_account(account: Account) -> "AccountOut":
        return AccountOut(
            id=account.id,
            platform=account.platform,
            name=account.name,
            is_active=account.is_active,
            created_at=account.created_at.isoformat(),
        )


class JobCreateOut(BaseModel):
    id: int


@app.get("/api/jobs", response_model=list[JobOut])
def list_jobs(
    status: JobStatus | None = None,
    platform: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(Job)
    if status is not None:
        query = query.filter(Job.status == status)
    if platform is not None:
        query = query.filter(Job.platform == platform)
    jobs = query.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit).all()
    return [JobOut.from_job(job) for job in jobs]


@app.get("/api/stats", response_model=StatsOut)
def stats(db: Session = Depends(get_db)):
    counts = dict(
        db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    )
    by_status = {s.value: counts.get(s, 0) for s in JobStatus}
    return StatsOut(total=sum(by_status.values()), by_status=by_status)


@app.post("/api/jobs/{job_id}/retry", response_model=RetryOut)
def retry_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
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
def list_accounts(platform: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Account)
    if platform is not None:
        query = query.filter(Account.platform == platform)
    accounts = query.order_by(Account.platform, Account.name).all()
    return [AccountOut.from_account(account) for account in accounts]


@app.post("/api/jobs", response_model=JobCreateOut, status_code=201)
async def create_job(
    platform: str = Form(...),
    file: UploadFile = File(...),
    account_id: int | None = Form(default=None),
    title: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """
    Upload-driven job creation for the dashboard's "New Job" tab. Builds the
    same payload shape scripts/enqueue_tiktok_test.py and
    enqueue_youtube_test.py do, then dispatches exactly like they do
    (publish_job.delay) — this route is a thin HTTP front end over that same
    pattern, not a new way of constructing jobs.
    """
    if platform not in _SUPPORTED_UPLOAD_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported platform {platform!r}; must be one of {sorted(_SUPPORTED_UPLOAD_PLATFORMS)}",
        )

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")

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
    elif platform == "tiktok":
        # Same rule app/publishers/tiktok.py enforces: no single-account
        # fallback (see scripts/enqueue_tiktok_test.py --account being
        # required, unlike YouTube's).
        raise HTTPException(status_code=400, detail="TikTok jobs require an account (no single-account fallback)")

    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename).name
    dest_path = _UPLOADS_DIR / f"{uuid.uuid4().hex}_{safe_name}"
    dest_path.write_bytes(await file.read())

    job_title = title.strip() if title and title.strip() else (
        f"Distribution engine upload {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )

    if platform == "tiktok":
        payload = {"video_path": str(dest_path), "title": job_title}
    else:
        payload = {"video_path": str(dest_path), "title": job_title, "privacy": "private"}

    job = Job(
        platform=platform,
        payload=payload,
        account_id=account.id if account else None,
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
