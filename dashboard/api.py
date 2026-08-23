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
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Job, JobStatus, WebhookEvent
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
