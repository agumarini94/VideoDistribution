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

import logging
from datetime import datetime

from sqlalchemy import update

from app.celery_app import celery_app
from app.config import settings
from app.db import SessionLocal
from app.exceptions import PermanentError, TransientError
from app.models import Account, Job, JobStatus
from app.notifications import send_alert
from app.publishers import fake as fake_publisher
from app.publishers import twitter as twitter_publisher
from app.publishers import youtube as youtube_publisher

logger = logging.getLogger(__name__)

# Per-platform publisher routing. Platforms without a real integration yet
# fall back to the fake publisher, so demo/test platforms keep working
# exactly as before. Adding a new real publisher later is a one-line change
# here, not a change to publish_job's retry/DLQ logic.
_PUBLISHERS_BY_PLATFORM = {
    "youtube": youtube_publisher.publish,
    "twitter": twitter_publisher.publish,
}

# Platforms whose publisher module exposes the proactive-refresh helpers
# (token_expires_within / refresh_stored_credentials), used by
# refresh_expiring_tokens (Phase 8). Twitter's OAuth 1.0a tokens don't
# expire, so it's absent here — only platforms with expiring OAuth2 tokens
# need an entry.
_TOKEN_REFRESH_MODULES_BY_PLATFORM = {
    "youtube": youtube_publisher,
}

# How far ahead of actual expiry refresh_expiring_tokens proactively
# refreshes a token (spec section 4, "Automated Token Refresh").
_TOKEN_REFRESH_WINDOW_SECONDS = 45 * 60


def _get_publisher(platform: str):
    return _PUBLISHERS_BY_PLATFORM.get(platform, fake_publisher.publish)


def _resolve_account_credentials(db, job: Job) -> dict | None:
    """
    Returns the JSON credentials for the job's Account, or None if the job
    has no account_id (single-account / env-var-only mode, unchanged from
    before Phase 6). Resolving the Account here — not inside the publisher
    — is what keeps publishers pure: they receive plain credential data,
    never a database session.

    A job pointing at a missing or deactivated account is a permanent
    configuration error, not something a retry would fix.
    """
    if job.account_id is None:
        return None

    account = db.get(Account, job.account_id)
    if account is None:
        raise PermanentError(f"Job {job.id} references account_id={job.account_id}, which does not exist")
    if not account.is_active:
        raise PermanentError(f"Account {account.id} ({account.name}) is inactive")
    return account.credentials


def _persist_refreshed_credentials(db, job: Job, result: object) -> None:
    """
    Publishers are pure and can't write to the database, so a publisher that
    refreshed its OAuth token mid-call (currently only youtube.py, see
    app/publishers/youtube.py Phase 7) surfaces the new credentials in its
    result dict instead. This is the one place that persists them back onto
    the Account row, keeping stored tokens fresh.
    """
    if job.account_id is None or not isinstance(result, dict):
        return
    refreshed_credentials = result.get("refreshed_credentials")
    if refreshed_credentials is None:
        return

    account = db.get(Account, job.account_id)
    if account is None:
        return
    account.credentials = refreshed_credentials
    db.commit()


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
            account_credentials = _resolve_account_credentials(db, job)
            result = publisher(job.platform, job.payload, account_credentials)
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
            _persist_refreshed_credentials(db, job, result)
            job.status = JobStatus.PUBLISHED
            db.commit()
    finally:
        db.close()


@celery_app.task
def handle_dead_letter(job_id: int, reason: str) -> None:
    """
    Task that receives jobs routed to the "dlq" queue.

    The job has already been persisted as FAILED by publish_job before it
    gets here; this task's job is to alert a human. We re-fetch the job to
    include platform/attempts in the alert, since publish_job only passes
    job_id and reason (the error message) as task args.
    """
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        platform = job.platform if job else "unknown"
        attempts = job.attempts if job else "unknown"
    finally:
        db.close()

    send_alert(
        f"Job #{job_id} moved to the dead-letter queue\n"
        f"Platform: {platform}\n"
        f"Attempts: {attempts}\n"
        f"Error: {reason}"
    )


@celery_app.task
def dispatch_due_jobs() -> None:
    """
    Celery Beat task (see beat_schedule in app/celery_app.py), runs every
    60s. Claims SCHEDULED jobs whose scheduled_at is due and dispatches them.

    The UPDATE ... RETURNING below is the row-level claim: it atomically
    flips status from SCHEDULED to QUEUED for due jobs in a single
    statement. If Beat fires this task again before a previous run
    finished, or multiple workers/beats exist later, only one execution's
    UPDATE can actually claim a given row — Postgres's row locking means a
    concurrent UPDATE targeting the same row waits, then finds status is no
    longer SCHEDULED and matches nothing, instead of both claiming it. This
    is safer than a SELECT-then-UPDATE, which would race.
    """
    db = SessionLocal()
    try:
        now = datetime.now()
        claim = (
            update(Job)
            .where(Job.status == JobStatus.SCHEDULED, Job.scheduled_at <= now)
            .values(status=JobStatus.QUEUED)
            .returning(Job.id)
        )
        claimed_ids = [row[0] for row in db.execute(claim)]
        db.commit()
    finally:
        db.close()

    for job_id in claimed_ids:
        publish_job.delay(job_id)


@celery_app.task
def refresh_expiring_tokens() -> None:
    """
    Celery Beat task (Phase 8, spec section 4 "Automated Token Refresh"),
    runs every 30 minutes (see beat_schedule in app/celery_app.py).
    Proactively refreshes OAuth tokens for active Accounts on platforms
    whose tokens expire, so a token doesn't die mid-publish.

    All OAuth mechanics (credential shape, calling refresh(), telling a
    revoked refresh_token apart from a transient network error) live in the
    relevant publisher module (see app/publishers/youtube.py) via
    _TOKEN_REFRESH_MODULES_BY_PLATFORM — this task only decides what to do
    with the outcome, same separation of concerns as publish_job.
    """
    db = SessionLocal()
    try:
        accounts = (
            db.query(Account)
            .filter(
                Account.platform.in_(_TOKEN_REFRESH_MODULES_BY_PLATFORM.keys()),
                Account.is_active.is_(True),
            )
            .all()
        )
        for account in accounts:
            _refresh_account_if_needed(db, account)
    finally:
        db.close()


def _refresh_account_if_needed(db, account: Account) -> None:
    module = _TOKEN_REFRESH_MODULES_BY_PLATFORM[account.platform]
    if not module.token_expires_within(account.credentials, _TOKEN_REFRESH_WINDOW_SECONDS):
        return

    try:
        account.credentials = module.refresh_stored_credentials(account.credentials)
        db.commit()
    except PermanentError as exc:
        # Refresh token is invalid/revoked: no amount of retrying fixes
        # this. Deactivate so publish_job stops dispatching jobs against it
        # (see _resolve_account_credentials) and alert a human — reviving
        # it requires a fresh interactive authorization.
        account.is_active = False
        db.commit()
        send_alert(
            f"Account #{account.id} ({account.platform}/{account.name}) needs re-authorization\n"
            f"Reason: {exc}\n"
            f'Run: python -m scripts.authorize_youtube --account "{account.name}" to reactivate it.'
        )
    except TransientError as exc:
        # Network blip or a transient error from the provider's token
        # endpoint: leave the account active, log, and let the next
        # scheduled run (30 min later) retry.
        logger.warning("Transient error refreshing account #%s (%s): %s", account.id, account.name, exc)
