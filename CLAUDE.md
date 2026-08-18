# distribution-engine

A queue-based content distribution engine for social networks. Jobs move
through a state machine (`scheduled -> queued -> processing -> published /
failed`; `scheduled` is optional), with exponential-backoff retries for
transient errors and a dead-letter queue for permanent ones.

## Architecture

- `app/models.py` — `Job` SQLAlchemy model: id, platform, payload (JSON),
  status, attempts, error_message, scheduled_at, created_at, updated_at.
  `JobStatus` enum models the state machine explicitly (including
  `SCHEDULED`, for jobs waiting on a future time slot).
- `app/publishers/` — **pure functions**. A publisher knows nothing about
  Celery (no retry/queue/job_id concepts) and never prints to screen. It
  either returns a result or raises a typed exception from
  `app/exceptions.py` (`TransientError` / `PermanentError`).
  - `fake.py` — simulates uploads: ~60% success, ~30% transient 429,
    ~10% permanent 400. Used for every platform without a real publisher.
  - `youtube.py` — real YouTube Data API v3 publisher (`videos.insert`).
    Loads OAuth2 credentials from `token.json` at the project root
    (generated once via `scripts/authorize_youtube.py`) and auto-refreshes
    them; raises `PermanentError` with a clear message if `client_secret.json`
    or `token.json` are missing, or if the refresh fails. Classifies
    `HttpError`s: HTTP 429/5xx and 403 quota/rate-limit reasons ->
    `TransientError`; other 4xx -> `PermanentError`.
- `app/tasks.py` — the only module that knows about both Celery and the
  publishers. `publish_job` looks up the right publisher for the job's
  `platform` (`_PUBLISHERS_BY_PLATFORM`, defaulting to the fake publisher),
  persists every state transition to the database, retries transient errors
  with exponential backoff (checking `self.request.retries >=
  self.max_retries` explicitly rather than catching
  `MaxRetriesExceededError`, which behaves inconsistently between eager and
  normal execution), and routes permanent errors (or exhausted retries) to
  `handle_dead_letter`, queued on `dlq`. `dispatch_due_jobs` is the Celery
  Beat task that claims due `SCHEDULED` jobs (see Phase 5).
- `app/celery_app.py` — the Celery app instance, kept separate from
  tasks.py to avoid import cycles. Also defines `PRIORITY_QUEUE_NAME`
  ("priority") and the `beat_schedule` entry for `dispatch_due_jobs`.
- `app/notifications.py` — `send_alert(message)` posts to a Discord/Slack
  incoming webhook (`ALERT_WEBHOOK_URL`), auto-detecting the payload shape
  by hostname. Same spirit as the publishers: self-contained, never raises
  (missing config or a webhook failure are logged and swallowed), so
  alerting can never break job processing. `handle_dead_letter` calls it
  with the job id, platform, attempts and error message.
- `app/storage.py` — Cloudflare R2 (S3-compatible) media staging via boto3:
  `upload_media`, `generate_signed_url`, `delete_media`. Self-contained,
  not wired into `app/tasks.py` yet (publishers will adopt it once a real
  media flow exists). Raises `StorageNotConfiguredError`
  (`app/exceptions.py`) if R2 credentials are missing, instead of failing
  cryptically inside boto3.
- `app/db.py` / `app/config.py` — SQLAlchemy session/engine and
  environment-based settings. No other module should read `os.environ`
  directly or import SQLAlchemy engine internals. `app/config.py` also
  holds `PLATFORM_TIME_SLOTS` and `next_slot_for(platform, now)` (Phase 5).

This separation (publisher / task / model) means adding a real platform
integration later never touches retry/DLQ logic, and publishers can be
unit-tested without Redis or a worker running.

## Conventions

- Code comments and log/print messages are in English.
- Publishers are pure: no side effects beyond raising typed exceptions or
  returning a result dict.
- Every job state change is persisted to the database before moving on.

## Stack

### Phase 1 (done)
- Python 3.11+
- Celery, broker: Redis (`localhost:6379` in development)
- SQLAlchemy + SQLite (dev-only, now retired — see Phase 2a)
- Fake publisher only, no real social platform integrations

### Phase 2a (current)
- **SQLAlchemy + PostgreSQL (Neon)** — replaces SQLite as the job store.
  `DATABASE_URL` is required (no local-file fallback); the app fails fast
  at startup if it's unset. Driver: `psycopg2-binary`.
- **Fly.io** (planned) — target deploy platform for the Celery worker(s)
  and any future API process. Not yet implemented.
- **Cloudflare R2** — see Phase 3 below for the storage module.

### Phase 2b (in progress)
- **YouTube publisher** (`app/publishers/youtube.py`) — built and wired
  into `app/tasks.py` (`platform="youtube"` routes to it, everything else
  still uses the fake publisher). Not yet tested end-to-end: the client
  hasn't provided real Google Cloud OAuth credentials
  (`client_secret.json`) yet, so `scripts/authorize_youtube.py` hasn't been
  run for real and the upload path is unverified against the live API.
  Code is written to fail with a clear `PermanentError` when credentials
  are absent rather than crash, so the rest of the system keeps working
  without them.

### Phase 3 (in progress)
- **Cloudflare R2 media staging** (`app/storage.py`) — S3-compatible object
  storage for media files (images/video) ahead of publishing: `upload_media`,
  `generate_signed_url` (presigned GET — what platform APIs will use to
  ingest media, per the spec) and `delete_media`, backed by boto3. Not
  wired into `app/tasks.py` yet; publishers will adopt it once a real media
  flow exists. Raises `StorageNotConfiguredError` if `R2_ENDPOINT_URL`,
  `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` or `R2_BUCKET_NAME` are
  missing, instead of failing cryptically inside boto3.
  `scripts/test_storage.py` verifies a bucket independently (upload, signed
  URL, delete).

  The spec's 7-day media lifecycle rule is **not implemented in code** — it
  must be configured as an object lifecycle (expiration) rule on the bucket
  itself, in the Cloudflare dashboard.

  **When R2 credentials arrive:**
  1. Set `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
     `R2_BUCKET_NAME` in `.env`.
  2. Run `python -m scripts.test_storage` to verify the bucket
     (upload/signed-url/delete round trip).
  3. In the Cloudflare dashboard, configure a 7-day object lifecycle
     (expiration) rule on the bucket — this is bucket config, not code.
  4. Wire `app/storage.py` into the relevant publisher(s) once a real
     media flow exists.

### Phase 4 (partial)
- **DLQ alerts via Discord/Slack webhook** (`app/notifications.py`) — every
  job that lands in the dead-letter queue triggers a `send_alert` call from
  `handle_dead_letter` with the job id, platform, attempts and error
  message. Configured via `ALERT_WEBHOOK_URL`; alerting is entirely
  optional and fails silently (logged, not raised) so it can never take
  down job processing. `scripts/test_alert.py` sends a one-off test alert
  to verify a webhook independently of a real DLQ event.

### Phase 5 (current)
- **Time-slot scheduling** (`app/config.py`) — `PLATFORM_TIME_SLOTS` (defaults:
  twitter 09:00/13:00/18:00, tiktok 12:00/19:00, youtube 15:00, everything
  else 12:00), overridable via `TIME_SLOTS`
  (`"twitter=09:00,13:00,18:00;tiktok=12:00,19:00"`). `next_slot_for(platform,
  now)` returns the next due datetime. Naive local time only — no
  per-account/platform timezone support yet, documented as a known
  limitation right in the code.
- **`JobStatus.SCHEDULED`** — jobs created with a time slot start here
  (`scheduled_at` set via `next_slot_for`) instead of `queued`.
- **`dispatch_due_jobs`** (`app/tasks.py`) — Celery Beat task, every 60s
  (`beat_schedule` in `app/celery_app.py`, requires running `celery -A
  app.celery_app beat` as its own process). Claims due jobs with a single
  atomic `UPDATE jobs SET status='queued' WHERE status='scheduled' AND
  scheduled_at <= now() RETURNING id` — the row-level claim that prevents
  double-dispatch if Beat overlaps itself or multiple workers exist later
  (Postgres row locking means only one execution's UPDATE can claim a
  given row) — then dispatches each claimed id with `publish_job.delay`.
- **Priority queue** (`"priority"`, `PRIORITY_QUEUE_NAME` in
  `app/celery_app.py`) — jobs created with `urgent=True` skip scheduling
  entirely (even if a time slot was also requested) and are dispatched
  immediately via `publish_job.apply_async(..., queue=PRIORITY_QUEUE_NAME)`.
  **Workers must listen with `-Q priority,celery,dlq`** (queue order =
  consumption preference) — see README.
- `scripts/enqueue_demo.py` — `--schedule` (jobs go to their next time
  slot) and `--urgent N` (first N jobs dispatch immediately via the
  priority queue, bypassing `--schedule`) flags. No flags = today's
  original behavior (10 jobs, immediate, normal queue).
- `scripts/show_jobs.py` — prints id/platform/status/attempts/scheduled_at
  for every job, to observe scheduling and priority dispatch without
  opening Neon directly.

## Monitoring dashboard (extra, not in the spec)

- `dashboard/` — a monitoring dashboard for the engine, built without any
  changes to `app/` (models, tasks, publishers). It only reads the same
  database (`SessionLocal` from `app/db.py`) and, for retries, calls
  `publish_job.delay` from `app/tasks.py`.
  - `dashboard/api.py` — FastAPI app: `GET /api/jobs` (filterable by
    `status`/`platform`, `limit` default 50, newest first), `GET
    /api/stats` (counts per `JobStatus` plus total), and `POST
    /api/jobs/{id}/retry` (only for jobs in `FAILED`: resets `status` to
    `QUEUED`, `attempts` to 0, clears `error_message`, commits, then
    dispatches `publish_job.delay(id)`; returns 409 for any other status).
    CORS is open for localhost. Also serves `dashboard/static/` so the
    whole dashboard runs from a single process.
  - `dashboard/static/index.html` — single-file vanilla JS frontend (no
    build step): stat cards per status, a filterable jobs table with
    status badges and a Retry button on failed rows, auto-refreshing every
    5s.
  - Run with `uvicorn dashboard.api:app --reload --port 8000`.
  - The dashboard is **read-only** except for the retry action.
  - Not committed yet (per instruction) — exists locally only.

## Running locally

See `README.md` for full setup and run instructions. Short version: Redis
running locally, `DATABASE_URL` set to a Neon connection string in `.env`,
then `celery -A app.celery_app worker --loglevel=info -Q priority,celery,dlq`,
`celery -A app.celery_app beat --loglevel=info`, and
`python -m scripts.enqueue_demo`.
