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


class Client(Base):
    """
    A workspace this engine publishes on behalf of (Phase 26) — either the
    operator's own brand ("individual") or an agency's managed client
    ("client"). Introduced to back the scheduler UI's client-switcher
    (design_handoff_scheduler/README.md): before this, a Job/Account's
    "client" was only informally encoded in Account.name (e.g. "Client X"),
    which the Queue/Calendar/Accounts screens can't reliably filter or
    display against. Deliberately minimal (name + kind only) — nothing here
    is read by app/tasks.py or any publisher; this is dashboard-only
    grouping metadata, same spirit as Account.credentials being opaque to
    everything except the publisher that reads it.
    """

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Free string, not an enum, same rationale as Job.platform: the
    # scheduler UI only distinguishes "individual" (the operator's own
    # brand) from "client" (an agency's managed client) today, but adding a
    # third kind shouldn't require a migration.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="client")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Client(id={self.id}, name={self.name!r}, kind={self.kind!r})"


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

    # Which Client workspace this connected account belongs to (Phase 26).
    # Nullable: accounts created before this phase, or created without a
    # workspace assigned, keep working exactly as before — nothing in
    # app/tasks.py or the publishers reads this, it's dashboard-only.
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Account(id={self.id}, platform={self.platform!r}, name={self.name!r})"


class User(Base):
    """
    A dashboard login for a client's team (Phase 28) — distinct from the
    ADMIN Basic-Auth credentials (DASHBOARD_USERNAME/PASSWORD,
    dashboard/api.py), which remain a separate, unchanged way to get full
    access (e.g. for curl/scripts/the auto-generated /docs). A User row is
    how a *named person* logs in, either as a client_user scoped to exactly
    one Client, or as an admin (see scripts/create_admin_user.py — nothing
    else can create a role="admin" row).

    Self-registration (POST /api/auth/register) always creates a
    role="client_user" row with is_approved=False and client_id=None: the
    requester doesn't know internal client_id values, so they type the
    client's name into requested_client_name instead, and an admin resolves
    that to a real Client (assigning client_id) when approving the request.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # bcrypt hash (see app/auth.py::hash_password) — never plaintext.
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    # Free string rather than an enum: only "admin"/"client_user" exist
    # today, but this avoids a schema migration if a third role is ever
    # needed (same rationale as Job.platform/Client.kind).
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="client_user")

    # Which Client workspace this user is scoped to. Null for role="admin"
    # rows (an admin isn't scoped to any one client) and for a still-pending
    # client_user registration (set at approval time, see
    # dashboard/api.py::approve_user).
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)

    # Free-text client name the user typed at registration — a hint for the
    # admin approving the request, not a validated reference (they don't
    # know internal client_id values, and exposing the full client roster
    # on a public, unauthenticated endpoint for lookup/autocomplete was
    # deliberately avoided; see CLAUDE.md Phase 28). Kept around after
    # approval for audit/display purposes; irrelevant once client_id is set.
    requested_client_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # False until an admin approves the registration (or the row was
    # created directly as an admin by scripts/create_admin_user.py, which
    # sets this True immediately — nothing else can create an admin row).
    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"User(id={self.id}, email={self.email!r}, role={self.role!r}, is_approved={self.is_approved})"


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

    # Which Client workspace this job was created for (Phase 26). Kept
    # independent of account_id rather than derived through Account.client_id
    # — a job can exist for a client before any Account is connected (single-
    # account/env-var fallback platforms like youtube/twitter), and the
    # scheduler UI's screens (Queue/Calendar/Composer) all scope by the
    # active client directly, not by an account's owner. Nullable: jobs
    # created before this phase, or through a script with no client concept,
    # keep working unchanged and simply show under no client.
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id"), nullable=True
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

    # Timestamp of the last "this job looks stalled" alert sent by
    # detect_stalled_jobs (Phase 14, app/tasks.py). Null if never alerted.
    # Read back before re-alerting so a job stuck for hours doesn't trigger
    # a fresh Discord/Slack message on every 10-minute Beat run — only once
    # settings.stall_realert_minutes has passed since this value.
    last_stall_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
