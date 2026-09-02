"""
Centralized configuration, read from environment variables.

Design decision: no other module should call os.environ directly. Keeping
config reading here means switching brokers or database engines only
requires touching this one file (plus the deploy's environment variables),
never the business logic.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Loads a .env file from the project root if present, so local development
# doesn't require exporting environment variables by hand. In production
# (Fly.io) the real environment variables are set directly and this call
# is a no-op if no .env file exists.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Redis is used as both the Celery broker and result backend (one Redis
    # instance is enough for this stage; no separate result backend needed).
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    celery_broker_url: str = os.getenv("CELERY_BROKER_URL", redis_url)
    celery_result_backend: str = os.getenv("CELERY_RESULT_BACKEND", redis_url)

    # Postgres (Neon) connection string. There is no local fallback default
    # on purpose: this project no longer ships a SQLite mode, so a missing
    # DATABASE_URL should fail loudly instead of silently writing to a local
    # file. See __post_init__ below.
    database_url: str = os.getenv("DATABASE_URL", "")

    # Maximum number of retries for transient errors (HTTP 429/5xx) before
    # the job is routed to the dead-letter queue.
    max_retries: int = int(os.getenv("MAX_RETRIES", "3"))

    # Base of the exponential backoff in seconds: attempt N waits base**N.
    # With base=2 and max_retries=3: 1s, 2s, 4s.
    retry_backoff_base: int = int(os.getenv("RETRY_BACKOFF_BASE", "2"))

    # Name of the queue that permanent-error jobs are routed to.
    dlq_queue_name: str = os.getenv("DLQ_QUEUE_NAME", "dlq")

    # Discord or Slack incoming webhook URL used by app/notifications.py to
    # alert on dead-letter jobs. Optional: an empty value disables alerting
    # without affecting job processing. .strip() guards against stray
    # whitespace in .env (e.g. "KEY= value").
    alert_webhook_url: str = os.getenv("ALERT_WEBHOOK_URL", "").strip()

    # How many minutes a job can sit in QUEUED or PROCESSING with no update
    # before app/tasks.py::detect_stalled_jobs (Phase 14) considers it
    # stalled and alerts.
    stall_threshold_minutes: int = int(os.getenv("STALL_THRESHOLD_MINUTES", "30"))

    # How many minutes must pass since a job's last stall alert before
    # detect_stalled_jobs alerts on it again — avoids re-alerting on every
    # Beat run (every 10 min) for a job that's still stuck.
    stall_realert_minutes: int = int(os.getenv("STALL_REALERT_MINUTES", "120"))

    # Cloudflare R2 (S3-compatible) credentials for app/storage.py. All
    # optional at this layer: unlike DATABASE_URL, a missing R2 config
    # shouldn't stop the whole app from starting, since storage isn't wired
    # into any publisher yet. app/storage.py itself raises a clear
    # StorageNotConfiguredError the moment one of these is actually needed.
    r2_endpoint_url: str = os.getenv("R2_ENDPOINT_URL", "").strip()
    r2_access_key_id: str = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    r2_secret_access_key: str = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    r2_bucket_name: str = os.getenv("R2_BUCKET_NAME", "").strip()

    # Public base URL of the R2 bucket (its r2.dev URL, or a custom domain
    # if one is ever attached), used by app/storage.py::upload_file to build
    # a publicly-fetchable URL for an uploaded file (public_url = this +
    # "/" + key). Only needed for upload_file; generate_signed_url doesn't
    # depend on it.
    r2_public_base_url: str = os.getenv("R2_PUBLIC_BASE_URL", "").strip()

    def __post_init__(self) -> None:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Copy .env.example to .env and set it "
                "to your Postgres (Neon) connection string."
            )


settings = Settings()


# --- Time-slot scheduling (Phase 5, timezone-aware since Phase 20) ---------
#
# NOTE ON TIMEZONES: PLATFORM_TIME_SLOTS times ("09:00", etc.) are the
# business's wall-clock intent, interpreted in SCHEDULER_TIMEZONE — not the
# timezone the server process happens to run in (which is UTC on Fly, but
# was silently "whatever the Mac's local timezone was" before this phase,
# a bug that only didn't bite because nothing had been deployed yet). All
# storage and comparisons stay in UTC: next_slot_for() converts the
# computed slot to aware UTC before returning it, so callers/DB columns
# never see SCHEDULER_TIMEZONE-local values.
#
# DST note: SCHEDULER_TIMEZONE transitions can shift or skip a wall-clock
# slot by an hour on the transition day, same as any other wall-clock
# schedule (e.g. a slot at a nonexistent or ambiguous local time on the
# transition day resolves per Python's normal zoneinfo fold/gap rules) —
# this is accepted, not specially handled.

_SCHEDULER_TIMEZONE_NAME = os.getenv("SCHEDULER_TIMEZONE", "UTC").strip() or "UTC"

try:
    SCHEDULER_TIMEZONE: ZoneInfo = ZoneInfo(_SCHEDULER_TIMEZONE_NAME)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(
        f"Invalid SCHEDULER_TIMEZONE {_SCHEDULER_TIMEZONE_NAME!r}: not a recognized IANA "
        "timezone name (e.g. 'UTC' or 'America/Argentina/Buenos_Aires')."
    ) from exc


def _ensure_utc(value: datetime) -> datetime:
    """
    Normalizes a datetime to timezone-aware UTC, treating a naive value as
    already-UTC — same convention (and reason: Postgres round-trips aware,
    SQLite round-trips naive) as app/tasks.py::_ensure_utc. Duplicated here
    rather than imported to avoid a config <-> tasks import cycle.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


_DEFAULT_PLATFORM_TIME_SLOTS: dict[str, list[str]] = {
    "twitter": ["09:00", "13:00", "18:00"],
    "tiktok": ["12:00", "19:00"],
    "youtube": ["15:00"],
    "default": ["12:00"],
}


def _parse_time_slots_env(raw: str) -> dict[str, list[str]]:
    """
    Parses TIME_SLOTS="twitter=09:00,13:00,18:00;tiktok=12:00,19:00" into a
    dict of platform -> list of "HH:MM" strings, validating each time.
    Platforms not mentioned keep their default slots (see
    _build_platform_time_slots below); this only overrides the ones listed.
    """
    slots: dict[str, list[str]] = {}
    for chunk in filter(None, (part.strip() for part in raw.split(";"))):
        if "=" not in chunk:
            raise RuntimeError(
                f"Invalid TIME_SLOTS entry {chunk!r}: expected 'platform=HH:MM,HH:MM'."
            )
        platform, times_raw = chunk.split("=", 1)
        platform = platform.strip().lower()
        times = [t.strip() for t in times_raw.split(",") if t.strip()]
        for t in times:
            try:
                datetime.strptime(t, "%H:%M")
            except ValueError as exc:
                raise RuntimeError(
                    f"Invalid time {t!r} for platform {platform!r} in TIME_SLOTS "
                    "(expected 24h HH:MM)."
                ) from exc
        slots[platform] = times
    return slots


def _build_platform_time_slots() -> dict[str, list[str]]:
    slots = dict(_DEFAULT_PLATFORM_TIME_SLOTS)
    raw = os.getenv("TIME_SLOTS", "").strip()
    if raw:
        slots.update(_parse_time_slots_env(raw))
    return slots


# platform (lowercase) -> list of "HH:MM" slots. Platforms with no entry
# fall back to PLATFORM_TIME_SLOTS["default"] (see next_slot_for).
PLATFORM_TIME_SLOTS: dict[str, list[str]] = _build_platform_time_slots()


def next_slot_for(platform: str, now: datetime) -> datetime:
    """
    Returns the next aware UTC datetime at which `platform` has a
    configured time slot, starting strictly after `now`: today's next slot
    (in SCHEDULER_TIMEZONE wall-clock terms) if one hasn't passed yet,
    otherwise tomorrow's earliest slot. `now` may be naive (assumed UTC,
    same convention as the rest of the app) or aware in any timezone.
    """
    now_local = _ensure_utc(now).astimezone(SCHEDULER_TIMEZONE)

    times = PLATFORM_TIME_SLOTS.get(platform.lower(), PLATFORM_TIME_SLOTS["default"])
    slot_times = sorted(datetime.strptime(t, "%H:%M").time() for t in times)

    for slot_time in slot_times:
        candidate_local = datetime.combine(now_local.date(), slot_time, tzinfo=SCHEDULER_TIMEZONE)
        if candidate_local > now_local:
            return candidate_local.astimezone(timezone.utc)

    # Every slot today has already passed (in SCHEDULER_TIMEZONE): tomorrow's
    # first one.
    tomorrow_local = now_local.date() + timedelta(days=1)
    candidate_local = datetime.combine(tomorrow_local, slot_times[0], tzinfo=SCHEDULER_TIMEZONE)
    return candidate_local.astimezone(timezone.utc)
