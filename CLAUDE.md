# distribution-engine

A queue-based content distribution engine for social networks. Jobs move
through a state machine (`queued -> processing -> published / failed`),
with exponential-backoff retries for transient errors and a dead-letter
queue for permanent ones.

## Architecture

- `app/models.py` — `Job` SQLAlchemy model: id, platform, payload (JSON),
  status, attempts, error_message, scheduled_at, created_at, updated_at.
  `JobStatus` enum models the state machine explicitly.
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
  `handle_dead_letter`, queued on `dlq`.
- `app/celery_app.py` — the Celery app instance, kept separate from
  tasks.py to avoid import cycles.
- `app/notifications.py` — `send_alert(message)` posts to a Discord/Slack
  incoming webhook (`ALERT_WEBHOOK_URL`), auto-detecting the payload shape
  by hostname. Same spirit as the publishers: self-contained, never raises
  (missing config or a webhook failure are logged and swallowed), so
  alerting can never break job processing. `handle_dead_letter` calls it
  with the job id, platform, attempts and error message.
- `app/db.py` / `app/config.py` — SQLAlchemy session/engine and
  environment-based settings. No other module should read `os.environ`
  directly or import SQLAlchemy engine internals.

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
- **Cloudflare R2** (planned) — target object storage for media assets
  attached to jobs (images/video) ahead of real publisher integrations.
  Not yet implemented.

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

### Phase 4 (partial)
- **DLQ alerts via Discord/Slack webhook** (`app/notifications.py`) — every
  job that lands in the dead-letter queue triggers a `send_alert` call from
  `handle_dead_letter` with the job id, platform, attempts and error
  message. Configured via `ALERT_WEBHOOK_URL`; alerting is entirely
  optional and fails silently (logged, not raised) so it can never take
  down job processing. `scripts/test_alert.py` sends a one-off test alert
  to verify a webhook independently of a real DLQ event.

## Running locally

See `README.md` for full setup and run instructions. Short version: Redis
running locally, `DATABASE_URL` set to a Neon connection string in `.env`,
then `celery -A app.celery_app worker --loglevel=info -Q celery,dlq` plus
`python -m scripts.enqueue_demo`.
