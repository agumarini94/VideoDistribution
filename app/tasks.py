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

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.celery_app import celery_app
from app.config import settings
from app.db import SessionLocal
from app.exceptions import PermanentError, TokenExpiredError, TransientError
from app.models import Account, Job, JobStatus, WebhookEvent
from app.notifications import send_alert
from app.publishers import facebook as facebook_publisher
from app.publishers import fake as fake_publisher
from app.publishers import instagram as instagram_publisher
from app.publishers import meta as meta_publisher
from app.publishers import tiktok as tiktok_publisher
from app.publishers import twitter as twitter_publisher
from app.publishers import youtube as youtube_publisher
from app.webhooks import tiktok as tiktok_webhooks

logger = logging.getLogger(__name__)

# Per-platform publisher routing. Platforms without a real integration yet
# fall back to the fake publisher, so demo/test platforms keep working
# exactly as before. Adding a new real publisher later is a one-line change
# here, not a change to publish_job's retry/DLQ logic.
_PUBLISHERS_BY_PLATFORM = {
    "youtube": youtube_publisher.publish,
    "twitter": twitter_publisher.publish,
    "tiktok": tiktok_publisher.publish,
    "facebook": facebook_publisher.publish,
    "instagram": instagram_publisher.publish,
}

# Platforms whose publisher module exposes the proactive-refresh helpers
# (token_expires_within / refresh_stored_credentials), used by
# refresh_expiring_tokens (Phase 8) AND (Phase 21, twitter; Phase 24,
# facebook; Phase 25, instagram) the reactive TokenExpiredError -> refresh
# -> retry path in publish_job below. Both facebook and instagram point at
# the same meta.py module (Phase 23) since both platforms share the same
# credential shape and refresh mechanics.
_TOKEN_REFRESH_MODULES_BY_PLATFORM = {
    "youtube": youtube_publisher,
    "tiktok": tiktok_publisher,
    "twitter": twitter_publisher,
    "facebook": meta_publisher,
    "instagram": meta_publisher,
}

# Human-readable re-authorization instructions per platform, named in the
# re-authorization alert (_deactivate_and_alert_for_reauth below) when a
# stored refresh token turns out to be permanently invalid/revoked.
# {name} is filled in with the Account's name. Twitter (Phase 21) has no
# interactive authorize script yet (unlike youtube/tiktok's
# scripts/authorize_*.py) — re-authorization means running X's OAuth 2.0
# PKCE flow by hand and registering the result via scripts/add_account.py.
_REAUTHORIZE_INSTRUCTIONS_BY_PLATFORM = {
    "youtube": 'python -m scripts.authorize_youtube --account "{name}"',
    "tiktok": 'python -m scripts.authorize_tiktok --account "{name}"',
    "twitter": (
        "obtain a fresh authorization code via X's OAuth 2.0 PKCE flow, then "
        'python -m scripts.add_account --platform twitter --name "{name}" '
        "client_id=... client_secret=... access_token=... refresh_token=... expires_at=..."
    ),
    "facebook": 'python -m scripts.authorize_meta --account "{name}" (re-links the Page and refreshes its user token)',
    "instagram": (
        'python -m scripts.authorize_meta --account "{name}" (re-authorizing the linked '
        "Facebook Page refreshes this Instagram account's credentials too)"
    ),
}

# How far ahead of actual expiry refresh_expiring_tokens proactively
# refreshes a token (spec section 4, "Automated Token Refresh"). Platform-
# specific because token lifetimes differ wildly (X's OAuth2 access tokens
# live ~2h, so they need a tighter window than a token that lives for a day
# or more) — entries not listed here fall back to the 45-minute default.
_TOKEN_REFRESH_WINDOW_SECONDS = 45 * 60
_TOKEN_REFRESH_WINDOW_SECONDS_BY_PLATFORM = {
    "twitter": 40 * 60,
    # Meta long-lived user tokens last ~60 days — far longer than any other
    # platform here, so they get a much wider proactive window (7 days)
    # instead of the 45-minute default.
    "facebook": 7 * 24 * 60 * 60,
    "instagram": 7 * 24 * 60 * 60,
}


def _get_publisher(platform: str):
    return _PUBLISHERS_BY_PLATFORM.get(platform, fake_publisher.publish)


def _ensure_utc(value: datetime) -> datetime:
    """
    Normalizes a datetime read back from the DB to timezone-aware UTC.
    Values are always written as aware UTC (see models.py::_utcnow), but
    Postgres round-trips them as aware while SQLite (used by the test
    suite) round-trips them as naive — comparing/subtracting an aware and a
    naive datetime raises. Same normalization pattern as
    app/publishers/youtube.py::token_expires_within.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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


def _persist_external_id(job: Job, result: object) -> None:
    """
    Every publisher returns the platform-assigned id for the published
    content as result["external_id"] (see app/publishers/*.py). Persisting
    it onto the job is what lets a later async event (currently: TikTok's
    webhook, Phase 10b) find its way back to the right Job — see
    handle_tiktok_webhook_event below. Does not commit; the caller commits
    alongside the status change.
    """
    if not isinstance(result, dict):
        return
    external_id = result.get("external_id")
    if external_id is not None:
        job.external_id = str(external_id)


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
            try:
                result = publisher(job.platform, job.payload, account_credentials)
            except TokenExpiredError as exc:
                # Refresh once and retry inline (not a Celery retry — this
                # doesn't consume one of self.max_retries). Whatever the
                # retried call raises (including PermanentError from a dead
                # refresh token, or another TransientError) is handled by
                # the same except clauses below, same as a first attempt.
                result = _handle_token_expired(db, job, publisher, exc)
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
            _persist_external_id(job, result)
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
        # Aware UTC, matching how scheduled_at is written (see
        # app/config.py::next_slot_for, Phase 20) and how Job.updated_at /
        # created_at are written (models.py::_utcnow) — comparing against a
        # naive local-time `now` here would silently misfire the moment
        # this runs somewhere other than UTC (e.g. Fly).
        now = datetime.now(timezone.utc)
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


def _deactivate_and_alert_for_reauth(db, account: Account, exc: Exception) -> None:
    """
    Shared outcome for "this account's refresh token is permanently
    invalid/revoked" — used by both the proactive Beat refresh
    (_refresh_account_if_needed) and the reactive refresh-on-401 path
    (_handle_token_expired). No amount of retrying fixes this: deactivate
    so publish_job stops dispatching jobs against it
    (_resolve_account_credentials) and alert a human, since reviving it
    requires a fresh out-of-band authorization.
    """
    account.is_active = False
    db.commit()
    instructions = _REAUTHORIZE_INSTRUCTIONS_BY_PLATFORM[account.platform].format(name=account.name)
    send_alert(
        f"Account #{account.id} ({account.platform}/{account.name}) needs re-authorization\n"
        f"Reason: {exc}\n"
        f"Run: {instructions} to reactivate it."
    )


def _refresh_account_if_needed(db, account: Account) -> None:
    module = _TOKEN_REFRESH_MODULES_BY_PLATFORM[account.platform]
    window = _TOKEN_REFRESH_WINDOW_SECONDS_BY_PLATFORM.get(account.platform, _TOKEN_REFRESH_WINDOW_SECONDS)
    if not module.token_expires_within(account.credentials, window):
        return

    try:
        account.credentials = module.refresh_stored_credentials(account.credentials)
        db.commit()
    except PermanentError as exc:
        _deactivate_and_alert_for_reauth(db, account, exc)
    except TransientError as exc:
        # Network blip or a transient error from the provider's token
        # endpoint: leave the account active, log, and let the next
        # scheduled run (30 min later) retry.
        logger.warning("Transient error refreshing account #%s (%s): %s", account.id, account.name, exc)


def _handle_token_expired(db, job: Job, publisher, exc: TokenExpiredError):
    """
    Reactive counterpart to _refresh_account_if_needed (Phase 21): the
    publisher itself just reported the access token it was given is
    expired/invalid (TokenExpiredError, see app/exceptions.py and
    app/publishers/twitter.py). Refreshes the Account's stored credentials,
    persists the rotated tokens, and retries the publish call once with the
    fresh credentials — a plain TransientError retry would just hit the
    same expired token again. Any exception the retried publish() call
    raises (including another TokenExpiredError) propagates to the caller
    (publish_job) and is handled exactly like a first-attempt failure.

    A job with no account_id can't be refreshed here: there's nowhere to
    persist a rotated refresh_token (see the "single-account mode" caveat
    in app/publishers/twitter.py's module docstring), so this re-raises the
    original error to fall through to normal transient backoff instead of
    silently doing nothing.
    """
    module = _TOKEN_REFRESH_MODULES_BY_PLATFORM.get(job.platform)
    if job.account_id is None or module is None or not hasattr(module, "refresh_stored_credentials"):
        logger.warning(
            "Job %s got a TokenExpiredError with no refreshable account (account_id=%s, platform=%s); "
            "treating as a normal transient error.",
            job.id, job.account_id, job.platform,
        )
        raise exc

    account = db.get(Account, job.account_id)
    if account is None:
        raise exc

    try:
        new_credentials = module.refresh_stored_credentials(account.credentials)
    except PermanentError as refresh_exc:
        _deactivate_and_alert_for_reauth(db, account, refresh_exc)
        raise PermanentError(
            f"Job {job.id}: access token expired and refreshing it failed, account deactivated: {refresh_exc}"
        ) from refresh_exc
    except TransientError:
        # Network blip refreshing: let the ORIGINAL TokenExpiredError drive
        # the normal transient/backoff path below, not the refresh attempt's
        # own (less informative) error.
        raise exc

    # Rotation (Phase 21): persist BOTH the new access token and the new
    # refresh token before using the access token for anything — losing the
    # rotated refresh_token here strands the account exactly as surely as
    # never refreshing at all.
    account.credentials = new_credentials
    db.commit()

    return publisher(job.platform, job.payload, new_credentials)


@celery_app.task
def handle_tiktok_webhook_event(webhook_event_id: int) -> None:
    """
    Processes a WebhookEvent row already stored by POST /webhooks/tiktok
    (dashboard/api.py, Phase 10b). Deliberately off the HTTP request path:
    the endpoint only verifies the signature and writes the audit row
    before dispatching this task, so a slow send_alert() call (an outbound
    HTTP request, see app/notifications.py) never delays the 200 response
    TikTok is waiting for.

    Matches the event to a Job via platform="tiktok" + external_id ==
    the event's publish_id (see _persist_external_id above for how that
    column gets populated). An event with no publish_id, or one that
    doesn't match any Job, is left as an audit-only row: already logged by
    the endpoint's 200 response, nothing more to do here — TikTok must not
    be made to retry something we can't resolve.
    """
    db = SessionLocal()
    try:
        event = db.get(WebhookEvent, webhook_event_id)
        if event is None:
            return

        if not event.publish_id:
            logger.info("TikTok webhook event #%s (%s) has no publish_id; nothing to match.", event.id, event.event_type)
            return

        job = db.query(Job).filter(Job.platform == "tiktok", Job.external_id == event.publish_id).one_or_none()
        if job is None:
            logger.info(
                "TikTok webhook event #%s (%s): no job found for publish_id=%s",
                event.id, event.event_type, event.publish_id,
            )
            return

        outcome = tiktok_webhooks.classify_event(event.event_type)
        if outcome == "failure":
            content_data = (event.raw_payload or {}).get("content") or {}
            if isinstance(content_data, str):
                try:
                    content_data = json.loads(content_data)
                except ValueError:
                    content_data = {}
            fail_reason = content_data.get("fail_reason") or event.event_type

            job.status = JobStatus.FAILED
            job.error_message = f"TikTok webhook reported failure ({event.event_type}): {fail_reason}"
            db.commit()

            send_alert(
                f"TikTok reported a publish failure for job #{job.id}\n"
                f"publish_id: {event.publish_id}\n"
                f"Event: {event.event_type}\n"
                f"Reason: {fail_reason}"
            )
        elif outcome == "success":
            # Idempotent: publish_job already set PUBLISHED right after the
            # upload succeeded (see publish_job above). This webhook is
            # TikTok's own later confirmation. Never resurrect a job a
            # failure event (or anything else) already marked FAILED.
            if job.status != JobStatus.FAILED:
                job.status = JobStatus.PUBLISHED
                db.commit()
        else:
            logger.info(
                "TikTok webhook event #%s: unrecognized event type %r for job #%s, no status change.",
                event.id, event.event_type, job.id,
            )
    finally:
        db.close()


@celery_app.task
def detect_stalled_jobs() -> None:
    """
    Celery Beat task (Phase 14, spec section 5 "queue stalls" alerting),
    runs every 10 minutes (see beat_schedule in app/celery_app.py). Finds
    jobs stuck in QUEUED or PROCESSING for longer than
    settings.stall_threshold_minutes and sends ONE alert listing all of
    them — a single Beat/worker outage can stall many jobs at once, and one
    combined alert is more useful (and less spammy) than one per job.

    Avoids re-alerting on every run for a job that's still stuck:
    Job.last_stall_alert_at (nullable, see app/models.py) is only updated
    when an alert actually fires for that job, and a job alerted within
    settings.stall_realert_minutes is skipped until that window passes.
    """
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        stall_cutoff = now - timedelta(minutes=settings.stall_threshold_minutes)
        realert_cutoff = now - timedelta(minutes=settings.stall_realert_minutes)

        stalled = (
            db.query(Job)
            .filter(Job.status.in_((JobStatus.QUEUED, JobStatus.PROCESSING)), Job.updated_at <= stall_cutoff)
            .all()
        )
        to_alert = [
            job
            for job in stalled
            if job.last_stall_alert_at is None or _ensure_utc(job.last_stall_alert_at) <= realert_cutoff
        ]
        if not to_alert:
            return

        lines = "\n".join(
            f"- Job #{job.id} ({job.platform}, {job.status.value}), stuck since {_ensure_utc(job.updated_at).isoformat()}"
            for job in to_alert
        )
        send_alert(
            f"{len(to_alert)} job(s) appear stalled (no progress for over "
            f"{settings.stall_threshold_minutes} min):\n{lines}"
        )

        for job in to_alert:
            job.last_stall_alert_at = now
        db.commit()
    finally:
        db.close()
