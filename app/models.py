"""
Data model for a publication job.

Design decision: the job state (JobStatus) explicitly models the spec's
state machine: scheduled -> queued -> processing -> published / failed
(scheduled is optional; jobs created without a time slot start at queued).
We don't scatter loose strings ("queued", "processing", ...) across the
code; everything goes through this enum so it's impossible to write
"pending" in one place and "queued" in another.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    """
    Credentials for one social-media account belonging to a client, scoped
    to a single platform. The start of multi-account support (Phase 6):
    a Job may optionally reference an Account (see Job.account_id below);
    jobs without one keep resolving credentials from env vars exactly as
    before, so this is purely additive.

    credentials is free-form JSON (like Job.payload) because each platform
    needs different fields (e.g. Twitter/X: access_token,
    access_token_secret) — validating its shape is the responsibility of
    the corresponding publisher, not this model.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Same free-string rationale as Job.platform: adding a platform
    # shouldn't require a schema migration.
    platform: Mapped[str] = mapped_column(String(50), nullable=False)

    # Human label to tell accounts on the same platform apart (e.g. "Main
    # brand account", "Client X"), shown by scripts/show_jobs.py.
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    credentials: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Lets an account be disabled (revoked/expired credentials, client
    # request) without deleting it or its history.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Account(id={self.id}, platform={self.platform!r}, name={self.name!r})"


class JobStatus(str, enum.Enum):
    """
    States of the job's state machine.

    scheduled  -> job created for a future time slot (scheduled_at set);
                  waiting for the dispatch_due_jobs Beat task to claim it
                  once scheduled_at is due. Skipped entirely for urgent jobs.
    queued     -> ready for a worker to pick it up right now.
    processing -> a worker is attempting to publish it right now.
    published  -> published successfully. Terminal state.
    failed     -> retries were exhausted (transient error) or the error was
                  permanent. Terminal state; the job is also routed to the
                  dead-letter queue for manual review.
    """

    SCHEDULED = "scheduled"
    QUEUED = "queued"
    PROCESSING = "processing"
    PUBLISHED = "published"
    FAILED = "failed"


class Job(Base):
    """
    Represents a request to publish content on a social network.

    payload is free-form JSON (text, images, hashtags, etc.) because each
    platform requires different fields; validating its shape is the
    responsibility of the corresponding publisher, not this model.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Name of the target social network (e.g. "twitter", "instagram"). It's a
    # free string rather than an enum because adding a new platform shouldn't
    # require a schema migration.
    platform: Mapped[str] = mapped_column(String(50), nullable=False)

    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Optional: which Account to publish as. Null means "no account on
    # record yet" — the publisher falls back to app-wide env-var
    # credentials (single-account mode), so existing jobs and platforms
    # without an Account row keep working unchanged. See app/tasks.py
    # (_resolve_account_credentials) and app/publishers/twitter.py.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True
    )

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=20),
        nullable=False,
        default=JobStatus.QUEUED,
    )

    # Number of publication attempts made (includes the first attempt, not
    # just the retries). Used for auditing and for deciding backoff.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Message of the last error seen (transient or permanent). Null if there
    # hasn't been a failed attempt yet.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Point in time at which a SCHEDULED job becomes due (see
    # app.config.next_slot_for). Null for jobs that were never scheduled
    # (created directly as queued, or dispatched urgently).
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Platform-assigned identifier for the published content (e.g. TikTok's
    # publish_id, a YouTube video id, a tweet id) — every publisher already
    # returns this as result["external_id"]; app/tasks.py persists it here
    # after a successful publish (see _persist_external_id). Null for jobs
    # published before this field existed. TikTok's webhook listener (Phase
    # 10b) is the first consumer: it matches an incoming event's publish_id
    # back to this column to find the Job it's about.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Job(id={self.id}, platform={self.platform!r}, status={self.status.value})"


class WebhookEvent(Base):
    """
    Audit trail of inbound platform webhook events (Phase 10b: TikTok
    Content Posting API status callbacks, POST /webhooks/tiktok in
    dashboard/api.py). Every request that passes signature verification is
    stored here — whether or not it can be matched to a Job — so nothing a
    platform sends is ever silently lost even if the matching/processing
    logic downstream has a bug.
    """

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Free string, same rationale as Job.platform: only "tiktok" today, but
    # adding another platform's webhook shouldn't require a schema change.
    platform: Mapped[str] = mapped_column(String(50), nullable=False)

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)

    # The platform's identifier for the content this event is about (e.g.
    # TikTok's publish_id), matched against Job.external_id. Nullable:
    # some event types aren't about a specific piece of content (e.g.
    # TikTok's authorization.removed).
    publish_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The full webhook envelope exactly as received (after JSON-decoding
    # the request body), kept verbatim for auditing/debugging regardless of
    # whether it was understood or matched to a job.
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"WebhookEvent(id={self.id}, platform={self.platform!r}, event_type={self.event_type!r})"
