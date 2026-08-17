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
  `app/exceptions.py` (`TransientError` / `PermanentError`). Currently only
  `fake.py` exists (simulates uploads: ~60% success, ~30% transient 429,
  ~10% permanent 400) — no real platform integrations yet.
- `app/tasks.py` — the only module that knows about both Celery and the
  publishers. `publish_job` persists every state transition to the
  database, retries transient errors with exponential backoff (checking
  `self.request.retries >= self.max_retries` explicitly rather than
  catching `MaxRetriesExceededError`, which behaves inconsistently between
  eager and normal execution), and routes permanent errors (or exhausted
  retries) to `handle_dead_letter`, queued on `dlq`.
- `app/celery_app.py` — the Celery app instance, kept separate from
  tasks.py to avoid import cycles.
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

## Running locally

See `README.md` for full setup and run instructions. Short version: Redis
running locally, `DATABASE_URL` set to a Neon connection string in `.env`,
then `celery -A app.celery_app worker --loglevel=info -Q celery,dlq` plus
`python -m scripts.enqueue_demo`.
