"""
Centralized configuration, read from environment variables.

Design decision: no other module should call os.environ directly. Keeping
config reading here means switching brokers or database engines only
requires touching this one file (plus the deploy's environment variables),
never the business logic.
"""

import os
from dataclasses import dataclass

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
