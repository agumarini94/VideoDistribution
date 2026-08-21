# distribution-engine

A queue-based content distribution engine for social networks. Jobs move
through a state machine (`scheduled -> queued -> processing -> published /
failed`; `scheduled` is optional), with exponential-backoff retries for
transient errors and a dead-letter queue for permanent ones.

## Architecture

- `app/models.py` — `Job` SQLAlchemy model: id, platform, payload (JSON),
  account_id (nullable FK to `Account`, see Phase 6), status, attempts,
  error_message, scheduled_at, created_at, updated_at. `JobStatus` enum
  models the state machine explicitly (including `SCHEDULED`, for jobs
  waiting on a future time slot). `Account` model (Phase 6): id, platform,
  name, credentials (JSON), is_active, created_at, updated_at — per-account
  credentials for platforms with multi-account support (currently just
  Twitter/X).
- `app/publishers/` — **pure functions**. A publisher knows nothing about
  Celery or the database (no retry/queue/job_id/Account-row concepts) and
  never prints to screen. It either returns a result or raises a typed
  exception from `app/exceptions.py` (`TransientError` / `PermanentError`).
  Signature: `publish(platform, payload, account_credentials=None) -> dict`
  — the third argument is plain credential data (or `None`) resolved by
  `app/tasks.py`, never a DB session (see Phase 6).
  - `fake.py` — simulates uploads: ~60% success, ~30% transient 429,
    ~10% permanent 400. Used for every platform without a real publisher.
    Ignores `account_credentials`.
  - `youtube.py` — real YouTube Data API v3 publisher (`videos.insert`).
    Loads OAuth2 credentials from `token.json` at the project root
    (generated once via `scripts/authorize_youtube.py`) and auto-refreshes
    them; raises `PermanentError` with a clear message if `client_secret.json`
    or `token.json` are missing, or if the refresh fails. Classifies
    `HttpError`s: HTTP 429/5xx and 403 quota/rate-limit reasons ->
    `TransientError`; other 4xx -> `PermanentError`. Ignores
    `account_credentials` for now (see Phase 6 — natural next candidate to
    migrate to per-account credentials).
  - `twitter.py` (Phase 6) — real X API v2 publisher (`POST /2/tweets` via
    tweepy, OAuth 1.0a user context). App-level `consumer_key`/`secret`
    always come from `X_API_KEY`/`X_API_SECRET`; the access token comes
    from `account_credentials` (`access_token`/`access_token_secret`) when
    the job has an account, otherwise falls back to
    `X_ACCESS_TOKEN`/`X_ACCESS_TOKEN_SECRET`. Classifies tweepy
    `HTTPException`s by status code the same way `youtube.py` does: 429/5xx
    -> `TransientError`; 401/403/other 4xx -> `PermanentError`. Missing
    app-level or access-token credentials -> `PermanentError` naming
    exactly what's missing. Payload: `{"text": str}` for now; media comes
    later via `app/storage.py`.
- `app/tasks.py` — the only module that knows about Celery, the publishers,
  *and* the `Account` table. `publish_job` looks up the right publisher for
  the job's `platform` (`_PUBLISHERS_BY_PLATFORM`, defaulting to the fake
  publisher), resolves `account_credentials` via `_resolve_account_credentials`
  (`None` if the job has no `account_id`; raises `PermanentError` if the
  referenced account is missing or `is_active=False`) and passes it as the
  publisher's third argument, persists every state transition to the
  database, retries transient errors with exponential backoff (checking
  `self.request.retries >= self.max_retries` explicitly rather than
  catching `MaxRetriesExceededError`, which behaves inconsistently between
  eager and normal execution), and routes permanent errors (or exhausted
  retries) to `handle_dead_letter`, queued on `dlq`. `dispatch_due_jobs` is
  the Celery Beat task that claims due `SCHEDULED` jobs (see Phase 5).
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
- `scripts/show_jobs.py` — prints id/platform/account/status/attempts/
  scheduled_at for every job (account is "-" when the job has no
  `account_id`), to observe scheduling, priority dispatch and per-account
  routing without opening Neon directly.

### Phase 6 (current)
- **Twitter/X publisher** (`app/publishers/twitter.py`) — built and wired
  into `app/tasks.py` (`platform="twitter"` routes to it). Not yet tested
  end-to-end: the client hasn't provided real X Developer Portal
  credentials yet. Code is written to fail with a clear `PermanentError`
  when credentials are absent (app-level or access-token) rather than
  crash, so the rest of the system keeps working without them.
- **Multi-account support, start of** (`app/models.py` `Account`) — a Job
  can optionally reference an `Account` (`account_id`, nullable) holding
  per-account `credentials` (JSON) for a platform. This is additive:
  existing jobs (and platforms without any `Account` rows, like the fake
  publisher and YouTube for now) keep resolving credentials from env vars
  exactly as before. `app/tasks.py::_resolve_account_credentials` is the
  only place that queries the `Account` table — publishers stay pure and
  never touch the database, they just receive the resolved credentials (or
  `None`) as a third argument. Twitter is the first publisher to actually
  use this; YouTube is documented as the natural next one to migrate to
  the same pattern once multi-account YouTube is needed.
- `scripts/add_account.py` — CLI to insert or update an `Account` row:
  `python -m scripts.add_account --platform twitter --name "Main account"
  access_token=... access_token_secret=...`. Matches on platform+name to
  decide insert vs. update (so re-running it rotates a token in place).

  **When X credentials arrive:**
  1. Set `X_API_KEY` and `X_API_SECRET` in `.env` (app-level, from the X
     Developer Portal).
  2. Either set `X_ACCESS_TOKEN`/`X_ACCESS_TOKEN_SECRET` in `.env` for
     single-account use, or run `scripts/add_account.py` to create one or
     more `Account` rows and put `account_id` on the relevant jobs.
  3. **Manual schema step (no Alembic yet, see "Qué falta" in README):**
     `init_db()`'s `create_all` only creates tables that don't exist yet —
     it will create the new `accounts` table on an existing Neon database,
     but it will **not** add the new `account_id` column to the existing
     `jobs` table. Run once, by hand, against Neon:
     ```sql
     ALTER TABLE jobs ADD COLUMN account_id INTEGER REFERENCES accounts(id);
     ```
  4. Test with a job whose `platform="twitter"` (and optionally
     `account_id` set) via `scripts/enqueue_demo.py` or a one-off insert,
     then confirm with `scripts/show_jobs.py`.

### Phase 7 (current)
- **YouTube migrated to multi-account credentials** — `youtube.py` now
  branches on `account_credentials` the same way `twitter.py` does:
  - Job has an `account_id`: `Credentials.from_authorized_user_info` builds
    OAuth2 credentials directly from `Account.credentials`, which must
    contain the same fields Google's `Credentials.to_json()` produces —
    `token`, `refresh_token`, `token_uri`, `client_id`, `client_secret`,
    `scopes` (optionally `expiry`). Missing/invalid fields ->
    `PermanentError` naming the problem.
  - Job has no `account_id`: unchanged single-account fallback to
    `token.json` / `client_secret.json` at the project root.
  - **Refresh persistence**: publishers are pure and can't write to the
    database, so when a token refresh happens against `account_credentials`,
    `publish()` includes the new credentials JSON in its result dict under
    `"refreshed_credentials"` (present only when a refresh actually
    happened). `app/tasks.py::publish_job` checks for that key after a
    successful publish and, if the job has an `account_id`, writes it back
    onto the `Account` row and commits
    (`_persist_refreshed_credentials`). In single-account mode the
    refreshed token is still written straight to `token.json`, as before —
    that path doesn't go through this mechanism.
- `scripts/authorize_youtube.py --account NAME` — runs the same interactive
  OAuth flow as before, but saves the resulting credentials onto an
  `Account` row (`platform="youtube"`, `name=NAME`) instead of `token.json`,
  via the same insert-or-update helper as `scripts/add_account.py`
  (`upsert_account`, now shared between the two scripts). Re-running it with
  the same `--account NAME` rotates that account's stored token in place.
  Without `--account`, behavior is unchanged (writes `token.json`).
- `scripts/enqueue_youtube_test.py` — replaces the inline `python -c`
  smoke-test blocks: `--video PATH` (required) and `--account NAME`
  (optional) create one `platform="youtube"` job (private, generated test
  title), linked to that `Account` if given, and dispatch it immediately.

  **Credentials JSON shape** (what both `Account.credentials` for a youtube
  account and `token.json` contain):
  ```json
  {
    "token": "...",
    "refresh_token": "...",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "...",
    "client_secret": "...",
    "scopes": ["https://www.googleapis.com/auth/youtube.upload"]
  }
  ```
  This is exactly `Credentials.to_json()`'s output — never hand-write it.

### Phase 8 (current)
- **Proactive OAuth token refresh** (spec section 4, "Automated Token
  Refresh") — `refresh_expiring_tokens` (`app/tasks.py`), a Celery Beat
  task scheduled every 30 minutes (`beat_schedule` in `app/celery_app.py`).
  Iterates active `Account` rows on platforms whose tokens expire
  (`_TOKEN_REFRESH_MODULES_BY_PLATFORM`, currently just `"youtube"` —
  Twitter's OAuth 1.0a access tokens don't expire, so it's intentionally
  absent). For each account whose stored token expires within the next 45
  minutes (`_TOKEN_REFRESH_WINDOW_SECONDS`) — or whose `expiry` is
  missing/unparseable, treated the same way — it refreshes and persists the
  new credentials, reusing the same "publisher returns data, task persists
  it" pattern as Phase 7's post-publish refresh.
  - All OAuth mechanics live in the publisher module, not the task: two new
    pure helpers in `app/publishers/youtube.py`,
    `token_expires_within(credentials, seconds)` (expiry check) and
    `refresh_stored_credentials(credentials)` (force-refresh, returns the
    new credentials dict or raises `TransientError`/`PermanentError`).
    `refresh_stored_credentials` classifies the error using google-auth's
    own `RefreshError.retryable` flag (network/5xx from the token endpoint
    -> `TransientError`; an invalid/revoked refresh token, e.g.
    `invalid_grant` -> `PermanentError`) rather than guessing from the
    message string.
  - On `PermanentError` (refresh token invalid/revoked):
    `_refresh_account_if_needed` sets the `Account`'s `is_active=False` (so
    `_resolve_account_credentials` stops routing jobs to it) and calls
    `send_alert` with the account name and a "needs re-authorization"
    message pointing at `scripts/authorize_youtube.py --account NAME`.
  - On `TransientError`: logged and skipped, no state change — the next
    scheduled run (30 min later) retries automatically.
  - **Reactivation**: re-running `scripts/authorize_youtube.py --account
    NAME` upserts the account via the shared `upsert_account` helper
    (`scripts/add_account.py`), which defaults `is_active=True` on both
    insert and update — so a fresh interactive authorization automatically
    revives a deactivated account, no separate "reactivate" step needed.
- `scripts/show_accounts.py` — companion to `scripts/show_jobs.py`: prints
  id/platform/name/active/token_expiry/updated_at for every `Account`, so
  token freshness and deactivation can be observed without opening Neon.

### Phase 9 (current)
- **Containerization + Fly.io deploy config** — build-and-run-locally only;
  no deployment happened (the Fly.io account belongs to the client).
  - `Dockerfile` — `python:3.13-slim`, non-root user, `WORKDIR /app`.
    `requirements.txt` is copied and installed before the rest of the code
    for layer caching. Default `CMD` runs the Celery worker (`-Q
    priority,celery,dlq`); `beat` and the dashboard `api` process reuse the
    same image with a different command (see `docker-compose.yml` /
    `fly.toml`). All configuration still comes from env vars via
    `app/config.py` — no secrets are baked into the image.
  - `.dockerignore` — excludes `.venv`, `.env`, `.git`, `*.mp4`,
    `token.json`, `client_secret.json`, the `celerybeat-schedule*` files,
    `__pycache__`, `.DS_Store`. `dashboard/` is **included** — it's meant
    to run in-container too, as the `api` process.
  - `fly.toml` — ready but unused: placeholder app name
    `"distribution-engine"`, `[processes]` with `worker`, `beat`, `api`
    (the last running `uvicorn dashboard.api:app --host 0.0.0.0 --port
    8000`), `internal_port 8000` on the `api` process only. Comments note
    that `REDIS_URL`, `DATABASE_URL`, `ALERT_WEBHOOK_URL`, the `X_*` and
    `R2_*` vars must all be set via `fly secrets set`, never in this file.
  - `docker-compose.yml` — local-only, not used by Fly.io. Three services
    (`worker`, `beat`, `api`), all building from the same `Dockerfile` with
    different commands, `env_file: .env`. Redis is not containerized — it
    keeps running directly on the Mac, and each service overrides
    `REDIS_URL=redis://host.docker.internal:6379/0` (documented inline)
    plus an `extra_hosts: host.docker.internal:host-gateway` entry so the
    same file also works on Linux, where that hostname isn't automatic.
    `DATABASE_URL` and everything else still comes from `.env`.
  - **Why YouTube in production requires multi-account mode**:
    `token.json` and `client_secret.json` are excluded from the image, so
    the single-account fallback in `app/publishers/youtube.py` can never
    work in a deployed container — any YouTube account used in production
    must exist as an `Account` row (Phase 7,
    `scripts/authorize_youtube.py --account NAME`), which is why Phase 7's
    multi-account migration mattered ahead of this phase.
  - Fly deploy checklist (documented, not executed): client creates the
    Fly.io account and the app (replacing the `fly.toml` placeholder
    name), run `fly secrets set` for every env var above, then `fly
    deploy`.

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
