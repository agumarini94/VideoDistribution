"""
Celery tasks: the distribution engine's orchestration layer.

Central design decision of this project: this is the ONLY module that knows
about both Celery and the publishers. The `publish_job` task:
  1. Persists every job state change to the database (queued -> processing
     -> published/failed), as required by the spec.
  2. Calls the publisher for the job's platform (a pure function, see
     app/publishers/) and translates the typed exceptions it can raise into
     infrastructure decisions: retry with exponential backoff
     (TransientError) or route to the dead-letter queue (PermanentError, or
     a TransientError that exhausted its retries).
The publisher knows nothing about any of this; if the retry policy changes
tomorrow (e.g. 5 attempts instead of 3), only this file needs to change.
"""

from app.celery_app import celery_app
from app.config import settings
from app.db import SessionLocal
from app.exceptions import PermanentError, TransientError
from app.models import Job, JobStatus
from app.publishers import fake as fake_publisher
from app.publishers import youtube as youtube_publisher

# Per-platform publisher routing. Platforms without a real integration yet
# fall back to the fake publisher, so demo/test platforms keep working
# exactly as before. Adding a new real publisher later is a one-line change
# here, not a change to publish_job's retry/DLQ logic.
_PUBLISHERS_BY_PLATFORM = {
    "youtube": youtube_publisher.publish,
}


def _get_publisher(platform: str):
    return _PUBLISHERS_BY_PLATFORM.get(platform, fake_publisher.publish)


def _mark_failed_and_deadletter(db, job: Job, error: Exception) -> None:
    """
    Shared transition for the two paths that end up in the dead-letter
    queue: a permanent error, or a transient error that exhausted its
    retries. Kept in one place so both paths persist and route identically.
    """
    job.status = JobStatus.FAILED
    job.error_message = str(error)
    db.commit()

    handle_dead_letter.apply_async(args=[job.id, str(error)])


@celery_app.task(bind=True, max_retries=settings.max_retries)
def publish_job(self, job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            # The job doesn't exist (e.g. it was deleted from the database
            # by hand). Nothing to retry and nowhere to persist an error.
            return

        job.status = JobStatus.PROCESSING
        job.attempts += 1
        db.commit()

        publisher = _get_publisher(job.platform)
        try:
            publisher(job.platform, job.payload)
        except TransientError as exc:
            job.error_message = str(exc)
            db.commit()

            # We check the limit BEFORE calling self.retry(): it's more
            # explicit and avoids depending on catching
            # MaxRetriesExceededError, whose exact semantics (which
            # exception actually reaches here) differ between normal
            # execution and eager mode. self.request.retries is the number
            # of retries already performed for this task.
            if self.request.retries >= self.max_retries:
                # Retries exhausted: a persistent transient error is treated
                # as permanent and routed to the dead-letter queue.
                _mark_failed_and_deadletter(db, job, exc)
            else:
                # Exponential backoff: attempt 0 -> 1s, attempt 1 -> 2s,
                # attempt 2 -> 4s (with the default retry_backoff_base=2).
                countdown = settings.retry_backoff_base**self.request.retries
                self.retry(exc=exc, countdown=countdown)
        except PermanentError as exc:
            # Permanent errors are never retried: they go straight to the DLQ.
            _mark_failed_and_deadletter(db, job, exc)
        else:
            job.status = JobStatus.PUBLISHED
            db.commit()
    finally:
        db.close()


@celery_app.task
def handle_dead_letter(job_id: int, reason: str) -> None:
    """
    Task that receives jobs routed to the "dlq" queue.

    The job has already been persisted as FAILED by publish_job before it
    gets here; this task is the extension point for whatever should happen
    with permanent errors later on (alerting, notifying a human, manual
    retries via an endpoint, etc.). For now it does nothing beyond existing
    as an explicit destination for the "dlq" queue, separate from normal
    processing.
    """
    return None
