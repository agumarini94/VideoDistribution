"""
Read-only monitoring dashboard for the distribution engine, plus a single
write action (retrying a failed job).

Design decision: this app only ever imports from app/ (SessionLocal, Job,
JobStatus, publish_job) and never the other way around, so the dashboard
stays a bolt-on layer that the engine has no knowledge of. It does not
touch app/models.py, app/tasks.py or the publishers.
"""

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Job, JobStatus
from app.tasks import publish_job

app = FastAPI(title="Distribution Engine Dashboard")

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


# Mounted last so it never shadows the /api/* routes above. html=True serves
# index.html for "/" (and for unmatched paths), so this stays a single-page app.
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
