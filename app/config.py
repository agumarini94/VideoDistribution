"""
Centralized configuration, read from environment variables.

Design decision: no other module should call os.environ directly. Keeping
config reading here means switching brokers or database engines only
requires touching this one file (plus the deploy's environment variables),
never the business logic.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

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

    def __post_init__(self) -> None:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Copy .env.example to .env and set it "
                "to your Postgres (Neon) connection string."
            )


settings = Settings()


# --- Time-slot scheduling (Phase 5) -----------------------------------------
#
# NOTE ON TIMEZONES: slot times and next_slot_for()'s `now` are naive local
# time (whatever timezone the server/process runs in). There is no
# per-platform or per-account timezone support yet, which is fine for a
# single-region deployment but will need real timezone handling before this
# runs somewhere with users/accounts across multiple timezones.

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
    Returns the next datetime (naive, local time) at which `platform` has a
    configured time slot, starting strictly after `now`: today's next slot
    if one hasn't passed yet, otherwise tomorrow's earliest slot.
    """
    times = PLATFORM_TIME_SLOTS.get(platform.lower(), PLATFORM_TIME_SLOTS["default"])
    slot_times = sorted(datetime.strptime(t, "%H:%M").time() for t in times)

    for slot_time in slot_times:
        candidate = datetime.combine(now.date(), slot_time)
        if candidate > now:
            return candidate

    # Every slot today has already passed: tomorrow's first one.
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, slot_times[0])
