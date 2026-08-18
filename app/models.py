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

from sqlalchemy import JSON, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Job(id={self.id}, platform={self.platform!r}, status={self.status.value})"
