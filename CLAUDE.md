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
  credentials for platforms with multi-account support (Twitter/X, YouTube,
  TikTok).
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
  - `twitter.py` (Phase 6, extended Phase 17, migrated to OAuth 2.0 + v2
    media upload in Phase 21) — real X API v2 publisher (`POST /2/tweets`
    via tweepy, OAuth 2.0 user-context Bearer auth). `account_credentials`
    (or the `TWITTER_*` env fallback) carries all four fields a
    confidential OAuth 2.0 client needs: `client_id`, `client_secret`,
    `access_token`, `refresh_token`. Classifies errors by HTTP status the
    same way `youtube.py` does — 429/5xx -> `TransientError`; other 4xx ->
    `PermanentError` — with one addition: a 401 flagged by X as an
    expired/invalid Bearer token raises `TokenExpiredError` (a
    `TransientError` subclass, see `app/exceptions.py`) instead of a plain
    `PermanentError`, so `app/tasks.py` can refresh and retry instead of
    dead-lettering a job over a token that just needed rotating. Payload
    (Phase 17): `{"text": str}` for a single tweet, optionally with
    `"media_paths"` (local file paths); or
    `{"thread": [{"text", "media_paths"?}, ...]}` for a thread — `"text"`
    and `"thread"` are mutually exclusive. Media still comes from local
    disk, not yet from `app/storage.py`. See Phase 17 below for the
    media-upload/threading details and Phase 21 for the OAuth 2.0
    migration and token-refresh flow.
  - `tiktok.py` (Phase 10) — real TikTok Content Posting API publisher,
    Sandbox mode: inbox-upload flow only (`POST
    /v2/post/publish/inbox/video/init/` then chunked `PUT` to the returned
    `upload_url`), since Sandbox only grants `video.upload` —
    `video.publish` (Direct Post) is gated behind app review. No
    single-account fallback: `account_credentials` is required (bearer
    `access_token`), from an `Account` row created via
    `scripts/authorize_tiktok.py`. Also exposes `token_expires_within` /
    `refresh_stored_credentials` (same shape as `youtube.py`) for
    `refresh_expiring_tokens`, and `exchange_authorization_code` for the
    one-time authorization script.
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
  now)` returns the next due datetime. Originally naive local time; made
  timezone-aware in Phase 20 (below) — slot times are now interpreted in
  `SCHEDULER_TIMEZONE` and the function returns aware UTC. Still no
  per-account/platform timezone support (one timezone applies to every
  slot), documented as a known limitation right in the code.
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
    for layer caching. Default `CMD` runs `scripts/start_all.sh` (made
    executable in the image via `RUN chmod +x`), which starts worker +
    beat + the dashboard api together in one container — see the
    single-machine mode note above. `docker-compose.yml`'s three services
    still override `command:` individually to run worker/beat/api as
    separate local containers (that file doesn't have Fly's
    separate-machine constraint, since compose containers on one Mac don't
    share `uploads/` either way without an explicit volume — out of scope
    here). All configuration still comes from env vars via
    `app/config.py` — no secrets are baked into the image.
  - `.dockerignore` — excludes `.venv`, `.env`, `.git`, `*.mp4`,
    `token.json`, `client_secret.json`, the `celerybeat-schedule*` files,
    `__pycache__`, `.DS_Store`. `dashboard/` is **included** — it's meant
    to run in-container too, as the `api` process.
  - `fly.toml` — ready but unused: placeholder app name
    `"distribution-engine"`. Comments note that `REDIS_URL`,
    `DATABASE_URL`, `ALERT_WEBHOOK_URL`, the `X_*` and `R2_*` vars must all
    be set via `fly secrets set`, never in this file.
  - **Single-machine mode (deliberate, pre-R2)**: `[processes]` has a
    single entry, `app = "scripts/start_all.sh"`, instead of separate
    `worker`/`beat`/`api` processes. Reason: the NEW JOB upload flow
    (`dashboard/api.py::create_job`, operator UI) writes files to local
    disk under `uploads/`, so the process that later reads that path (the
    worker) must share a filesystem with the process that wrote it (the
    api process) — on Fly, separate `[processes]` entries can land on
    separate machines, so they can't safely split while uploads live on
    local disk. `scripts/start_all.sh` (bash, `set -e`) starts the Celery
    worker (`-Q priority,celery,dlq`) and Celery beat in the background,
    logs a line per service started, then `exec`s `uvicorn
    dashboard.api:app --host 0.0.0.0 --port 8000` in the foreground so it
    becomes PID 1. **Split back into separate `worker`/`beat`/`api`
    processes once R2 storage (Phase 3) is wired into the upload flow** and
    uploads no longer live on local disk. `[[services]]` targets this one
    `"app"` process; `internal_port 8000` unchanged.
  - **`GET /health`** (`dashboard/api.py`) — liveness probe, exempt from
    HTTP Basic auth the same way the TikTok webhook route is (Phase 11):
    runs a cheap `SELECT 1` and reports `db: "ok"/"error"`, but always
    returns HTTP 200 (even if the DB check fails) since this is a liveness
    probe, not a readiness/dependency check — a transient DB blip
    shouldn't get the machine restarted by Fly. `fly.toml`'s
    `http_checks.path` points here instead of `/api/stats` (which would've
    required auth or an exemption of its own, and returns real data on
    every probe for no reason).
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

### Phase 10 (current)
- **TikTok publisher** (`app/publishers/tiktok.py`) — built and wired into
  `app/tasks.py` (`platform="tiktok"` routes to it). **Sandbox mode only**:
  the app's TikTok Developer Portal review hasn't passed, so it only has the
  `video.upload` scope — `video.publish` is gated behind review. As a
  result the publisher implements the **inbox-upload flow**
  (`POST /v2/post/publish/inbox/video/init/`, then chunked `PUT` to the
  returned `upload_url`): the video lands as a **draft in the target
  account's TikTok inbox**, not a live post — the account owner has to open
  the TikTok app and manually publish it. It also only works against
  Sandbox target accounts registered as testers for this app in the
  Developer Portal, and getting the app itself listed publicly requires
  submitting a demo video showing the full posting flow as part of the
  review.
  - **Direct Post migration path**: the inbox-vs-Direct-Post difference is
    isolated to the init endpoint and request body (`_INBOX_INIT_URL` /
    `_DIRECT_POST_INIT_URL` and `_build_init_body` in `tiktok.py`, with an
    inline comment marking the swap) — the chunked-upload mechanics are
    identical either way, so switching once `video.publish` is approved is
    a small, contained change, not a rewrite.
  - **No single-account fallback**: unlike `youtube.py`/`twitter.py`,
    `tiktok.py` has no env-var fallback credential path (there's no TikTok
    equivalent of `token.json` or `X_ACCESS_TOKEN`) — every `platform="tiktok"`
    job needs an `account_id`, resolved the same way as the other platforms
    via `_resolve_account_credentials`.
  - Error classification, consistent with `youtube.py`/`twitter.py`: HTTP
    429/5xx -> `TransientError`; other 4xx (invalid params, bad/expired
    auth) -> `PermanentError`; missing credentials -> `PermanentError`
    naming exactly what's missing. The Content Posting API reports most
    logical errors as HTTP 200 with a nested `error.code` (`_raise_for_api_error`),
    while the OAuth token endpoint reports them as top-level
    `error`/`error_description` strings (`_raise_for_token_error`) — these
    are two different response shapes and are classified separately on
    purpose.
  - **Proactive token refresh**: exposes `token_expires_within` /
    `refresh_stored_credentials`, same contract as `youtube.py`, registered
    in `_TOKEN_REFRESH_MODULES_BY_PLATFORM` (`app/tasks.py`) so
    `refresh_expiring_tokens` (Phase 8) manages TikTok accounts the same
    way it manages YouTube ones. The re-authorization alert on a
    revoked/invalid refresh token now looks up the right script per
    platform (`_REAUTHORIZE_SCRIPT_BY_PLATFORM` in `app/tasks.py`) instead
    of hardcoding `scripts/authorize_youtube.py`.
- `scripts/authorize_tiktok.py --account NAME` (required, no default) —
  interactive OAuth: opens a browser for the authorization URL
  (`video.upload` + `user.info.basic` scopes), waits for the authorization
  code, exchanges it for tokens via `tiktok.py`'s
  `exchange_authorization_code`, and upserts an `Account` row
  (`platform="tiktok"`) via the same `upsert_account` helper as the other
  authorize scripts. Prints setup instructions if `TIKTOK_CLIENT_KEY` /
  `TIKTOK_CLIENT_SECRET` / `TIKTOK_REDIRECT_URI` are missing.
  - **`TIKTOK_REDIRECT_URI` is a public HTTPS forwarder page, not
    localhost**: the TikTok Developer Portal rejects
    localhost/127.0.0.1 redirect URIs outright, so the registered URI has
    to be a real public page. The trick: a static page (e.g. published via
    GitHub Pages) whose entire content is a `location.replace()` that
    forwards the callback's query string to
    `http://localhost:8910/callback`:
    ```html
    <!doctype html>
    <script>
      location.replace("http://localhost:8910/callback" + location.search);
    </script>
    ```
    That public page's URL is what's registered in the Portal and what's
    set as `TIKTOK_REDIRECT_URI` — it's sent to TikTok verbatim (authorize
    URL + token exchange, where it must match the Portal exactly) but the
    script itself never connects to it. `_wait_for_callback(port)` in the
    script instead always binds a one-shot local HTTP server to
    `localhost:TIKTOK_LOCAL_CALLBACK_PORT` (env var, default `8910`,
    path `/callback`) — that's the actual target the forwarder page's
    `location.replace()` hits, independent of whatever
    `TIKTOK_REDIRECT_URI` is set to. The literal `8910` hardcoded in the
    forwarder page's JS has to match `TIKTOK_LOCAL_CALLBACK_PORT` — if that
    env var changes (e.g. port conflict), the published page must be
    updated too.
  - **PKCE (required by TikTok's OAuth, unlike Google's/X's) — and
    non-standard**: `_generate_pkce_pair()` in the script generates a
    `code_verifier` (`secrets.token_urlsafe`) and a `code_challenge`, sent
    as `code_challenge`/`code_challenge_method=S256` on the authorize URL.
    The challenge is the **hex** digest of SHA-256(verifier)
    (`hashlib.sha256(...).hexdigest()`), *not* RFC 7636's
    BASE64URL(SHA256(verifier)) — TikTok's Login Kit for Desktop docs
    (developers.tiktok.com/doc/login-kit-desktop) require the hex form, and
    the standard base64url form gets rejected by TikTok's login page with a
    `code_challenge` error. This deviation is deliberate and documented
    in-code precisely so it doesn't get "corrected" back to base64url
    later. The verifier is kept in memory (never sent until the token
    exchange) and passed straight into
    `exchange_authorization_code(code, redirect_uri, code_verifier)`, which
    includes it as `code_verifier` in the token request body — TikTok
    recomputes hex(SHA256(verifier)) server-side and checks it against the
    challenge it received earlier.

  **When TikTok credentials arrive:**
  1. Publish the forwarder page (see above) somewhere public over HTTPS
     (GitHub Pages is the easy option) — its URL is `TIKTOK_REDIRECT_URI`.
  2. In the TikTok for Developers portal, create/select the app, add the
     target account(s) as Sandbox testers, register that forwarder page's
     URL under Login Kit, and request the `video.upload` scope.
  3. Set `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REDIRECT_URI`
     (the forwarder page URL) in `.env`. `TIKTOK_LOCAL_CALLBACK_PORT` only
     needs setting if `8910` is taken locally (and then the forwarder
     page's JS must be updated to match).
  4. Run `python -m scripts.authorize_tiktok --account NAME` per Sandbox
     account.
  5. Test with a job whose `platform="tiktok"` and `account_id` set to that
     account — the video lands as a draft in the account's TikTok inbox,
     not a live post (Sandbox limitation, see above).
  6. When ready for production: submit the app for review (including the
     required demo video of the full posting flow) to get `video.publish`
     granted, then switch `tiktok.py` to the Direct Post endpoint per the
     migration path documented above.

### Phase 10b (current)
- **TikTok webhook listener** — `POST /webhooks/tiktok`, added to the same
  FastAPI app as the dashboard (`dashboard/api.py`), for TikTok's Content
  Posting API status callbacks. Built fully testable locally with curl /
  `scripts/simulate_tiktok_webhook.py` since the Developer Portal blocks
  registering a real Sandbox webhook URL right now (see Phase 10's "Live
  test pending" note) — there is no live payload to have observed yet.
  - **Event-naming caveat**: the only Content Posting-adjacent webhook
    events documented at developers.tiktok.com/doc/webhooks-events are
    `video.upload.failed` and `video.publish.completed` (envelope carries
    the content identifier in a field migrated from `share_id` to
    `publish_id`). This does **not** match the `post.publish.*` naming
    sometimes seen elsewhere in TikTok's docs/marketing. Since a real
    Sandbox payload can't be observed yet to settle this,
    `app/webhooks/tiktok.py::classify_event` matches by substring
    (`"failed"`/`"fail"` -> failure, `"completed"`/`"delivered"`/`"success"`
    -> success) instead of an exhaustive hardcoded event list — it should
    keep working under either naming scheme without a code change once a
    real webhook can be registered and observed. Re-verify this the first
    time a real event arrives (see the "what remains" note below).
  - **Signature verification** (`app/webhooks/tiktok.py::verify_signature`,
    per developers.tiktok.com/doc/webhooks-verification): the
    `TikTok-Signature` header is `t=<unix_ts>,s=<hex hmac-sha256>`; the
    signed message is `<unix_ts>.<raw_json_body>`, HMAC-SHA256'd with
    `TIKTOK_CLIENT_SECRET`. Verifies against the raw request bytes (not a
    re-serialized dict, which isn't guaranteed to reproduce them), and
    rejects timestamps older than `TIKTOK_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS`
    (default 300s) as a replay guard. A failed verification is a 401, not a
    200 — unlike an unresolvable `publish_id`, a bad signature is a
    security-relevant rejection that must not be silently masked (TikTok's
    own retry-with-backoff gives a legitimate-but-misfiring request another
    chance).
  - **`TIKTOK_WEBHOOK_SKIP_SIGNATURE=1`** disables verification entirely —
    local curl/`scripts/simulate_tiktok_webhook.py` testing only.
    `dashboard/api.py` logs a loud warning banner at process startup
    whenever it's set, so it can't accidentally go unnoticed in a deploy.
  - **Matching**: every publisher already returns the platform's id for the
    published content as `result["external_id"]` (`app/publishers/*.py`) —
    `publish_job` now persists it onto `Job.external_id`
    (`_persist_external_id`, `app/tasks.py`), which is what the webhook
    matches against. **Manual schema step**, same pattern as Phase 6's
    `account_id`: `init_db()`'s `create_all` only creates tables that don't
    exist yet, so on an existing Neon database this creates the new
    `webhook_events` table but does **not** add `jobs.external_id`. Run
    once, by hand, against Neon:
    ```sql
    ALTER TABLE jobs ADD COLUMN external_id VARCHAR(255);
    ```
    Jobs published before this column existed have `external_id = NULL`
    and can't be matched retroactively.
  - **Processing is off the request path**: the route
    (`dashboard/api.py::tiktok_webhook`) only verifies the signature,
    parses the envelope, inserts a `WebhookEvent` audit row, and dispatches
    `handle_tiktok_webhook_event.delay(...)` — a Celery task
    (`app/tasks.py`) that does the actual `Job` lookup, status transition,
    and `send_alert` call. This keeps the HTTP response fast regardless of
    Discord/Slack latency, matching TikTok's requirement to respond 200
    promptly (docs say it retries with backoff for up to 72h on anything
    else, with at-least-once delivery — processing is written to be
    idempotent: a success event never resurrects a job a failure event
    already marked `FAILED`, and re-processing the same event just
    re-applies the same transition).
  - **Outcomes**: a failure-classified event sets the job `FAILED` with
    `error_message` from the event (preferring `content.fail_reason` when
    present) and calls `send_alert` — same Discord/Slack channel as the
    dead-letter queue, but this path does **not** go through
    `handle_dead_letter`/the `dlq` queue, since this isn't a publish
    attempt that could be retried, it's TikTok's own after-the-fact status
    report on content it already accepted. A success-classified event is a
    no-op if the job is already `PUBLISHED` (the common case — `publish_job`
    already marks it `PUBLISHED` right after the chunked upload succeeds;
    this webhook is TikTok's later confirmation). An unrecognized event
    type, or one with no `publish_id`, or one whose `publish_id` matches no
    `Job`, is logged and left as an audit-only `WebhookEvent` row — nothing
    else happens, and the HTTP layer already answered 200 so TikTok won't
    retry it.
  - `app/models.py::WebhookEvent` — the audit table: `platform`,
    `event_type`, `publish_id` (nullable), `raw_payload` (the full envelope,
    JSON), `received_at`. Every request that passes signature verification
    is stored here regardless of whether it could be parsed/matched.
  - `scripts/simulate_tiktok_webhook.py` — builds a real
    `TikTok-Signature` header from `TIKTOK_CLIENT_SECRET` and POSTs one of
    three scenarios (`--scenario delivered|failed|unknown`) with a given
    `--publish-id` against a running dashboard instance. `--skip-signature`
    tests the `TIKTOK_WEBHOOK_SKIP_SIGNATURE=1` path instead. See README
    for example invocations.
  - **What remains** (out of scope here, same blocker as Phase 10's live
    test): registering the real callback URL in the TikTok Developer
    Portal once Sandbox/app-review access allows it, and re-verifying the
    event-naming assumption above against a real payload the first time
    one arrives.

### Phase 11 (current)
- **HTTP Basic auth for the dashboard** (pre-deploy hardening, not in the
  original spec) — `dashboard/api.py` adds a single `@app.middleware("http")`
  (`enforce_basic_auth`) that protects every route uniformly: `/api/*`, the
  `StaticFiles` mount (the frontend), and FastAPI's auto-generated `/docs`,
  `/redoc`, `/openapi.json`. A middleware was used instead of a per-route
  `Depends` specifically so nothing new can be added later and accidentally
  ship unauthenticated — `StaticFiles` and the auto docs routes don't take a
  `Depends` the way a normal path operation does.
  - **`POST /webhooks/tiktok` is exempt by path** (`_WEBHOOK_PATH` in
    `dashboard/api.py`) — TikTok's servers can't supply dashboard
    credentials, and the route already has its own auth (the
    `TikTok-Signature` verification from Phase 10b), so exempting it doesn't
    reduce security.
  - **Credentials**: `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` env vars,
    compared with `secrets.compare_digest` (both comparisons always run, so
    a wrong username doesn't short-circuit before the password comparison
    and leak timing information). Built on `fastapi.security.HTTPBasic`.
  - **Fail-open for local dev, loud otherwise**: if both vars are set, auth
    is enforced. If either is missing, the app still starts (so a fresh
    clone with no `.env` still runs) but logs an unmissable startup warning
    banner — same style as the existing
    `TIKTOK_WEBHOOK_SKIP_SIGNATURE` warning (Phase 10b) — since an
    unprotected dashboard exposes job/account data and the retry action to
    anyone who can reach the process.
  - **Middleware ordering matters**: `enforce_basic_auth` is registered
    *before* `CORSMiddleware`'s `app.add_middleware(...)` call, so CORS ends
    up wrapping around it (Starlette's middleware stack runs the
    most-recently-added middleware outermost/first). This keeps CORS
    preflight `OPTIONS` requests — which never carry an `Authorization`
    header — handled by `CORSMiddleware` before they'd otherwise hit the
    auth check and get rejected.

### Phase 12 (current)
- **First automated test suite** (`tests/`, pytest) — tests only, no
  behavior changes to existing code. Covers, in priority order:
  `app/webhooks/tiktok.py` (signature verification, envelope parsing, event
  classification), error classification in `app/publishers/tiktok.py` and
  `app/publishers/twitter.py` (HTTP 429/500/400/401 and TikTok's
  200-with-nested-error-code shape, plus a happy-path chunked upload), PKCE
  pair generation in `scripts/authorize_tiktok.py`, and
  `app/tasks.py::handle_tiktok_webhook_event`'s matching/idempotency logic.
  No network calls (HTTP mocked with `responses` or monkeypatch) and no
  Celery worker (task functions are called directly). `tests/conftest.py`
  points `DATABASE_URL` at a throwaway SQLite file *before* any `app/`
  module is imported (`app/config.py` reads it into a frozen `Settings` at
  import time), so the suite never touches the real Neon database, and
  resets all tables after every test since tasks under test open their own
  `SessionLocal()` sessions (a rollback-only fixture wouldn't undo those
  commits). `pytest` and `responses` live in `requirements-dev.txt`
  (`-r requirements.txt` plus the two), kept out of `requirements.txt` so
  the production image doesn't carry test-only dependencies. Not yet
  covered: `dashboard/api.py` (the FastAPI routes themselves, incl. the
  `/webhooks/tiktok` endpoint and HTTP Basic auth), `youtube.py`, `fake.py`,
  time-slot scheduling, and the retry/backoff/DLQ logic in
  `publish_job` — natural next candidates when the suite grows.

### Phase 13 (current)
- **X (Twitter) 280-character pre-flight guard** — spec-audit gap fix, no
  behavior change beyond this one validation.
  `app/publishers/twitter.py::_validate_payload` now rejects any `text`
  over 280 characters with a `PermanentError` naming the actual length,
  before any HTTP call is made. Counts with plain `len()`; a comment in
  the code notes this is an approximation, since X counts every URL as a
  fixed 23 characters (its `t.co` wrapper) regardless of its real length —
  a tweet whose text is mostly a very long URL can pass this check and
  still be rejected upstream. Accepted as a known limitation rather than
  reimplementing X's URL-counting rules. Covered by
  `tests/test_publisher_twitter.py::TestCharacterLimit` (280 exactly
  passes and reaches the mocked API; 281 raises `PermanentError` without
  any HTTP call being made).

### Phase 14 (current)
- **Queue stall detection** (spec section 5, "queue stalls" alerting) —
  `detect_stalled_jobs` (`app/tasks.py`), a Celery Beat task scheduled
  every 10 minutes (`beat_schedule` in `app/celery_app.py`, same pattern
  as `dispatch_due_jobs`/`refresh_expiring_tokens` — no separate process
  needed). Queries jobs in `QUEUED` or `PROCESSING` whose `updated_at` is
  older than `settings.stall_threshold_minutes` (env `STALL_THRESHOLD_MINUTES`,
  default 30) and sends **one** `send_alert` call listing all of them
  (id, platform, status, how long they've been stuck) — a single
  Beat/worker outage can stall many jobs at once, and one combined alert
  beats flooding the channel with one per job.
  - **Anti-spam re-alerting**: `Job.last_stall_alert_at` (new nullable
    column, `app/models.py`) is set only when an alert actually fires for
    that job; a job whose `last_stall_alert_at` is within
    `settings.stall_realert_minutes` (env `STALL_REALERT_MINUTES`, default
    120) of now is skipped, so a job stuck for hours doesn't generate a
    fresh alert on every 10-minute run — only once the re-alert window has
    passed.
  - **Timezone normalization**: `app/tasks.py::_ensure_utc` treats a
    naive datetime read back from the DB as UTC before comparing/
    subtracting against `datetime.now(timezone.utc)` — needed because
    Postgres round-trips `DateTime(timezone=True)` columns as aware but
    SQLite (the test suite's DB) round-trips them as naive. Same pattern
    already used by `app/publishers/youtube.py::token_expires_within`.
    The SQL-side cutoff filter (`Job.updated_at <= stall_cutoff`) doesn't
    need this — comparisons executed by the DB engine aren't affected by
    Python-side tzinfo, only the Python-side re-alert check and the
    alert's stuck-since timestamps are.
  - **Manual schema step**, same pattern as `account_id` (Phase 6) and
    `external_id` (Phase 10b): `init_db()`'s `create_all` only creates
    tables that don't exist yet, so on an existing Neon database this
    does **not** add `last_stall_alert_at` to the existing `jobs` table.
    Run once, by hand, against Neon:
    ```sql
    ALTER TABLE jobs ADD COLUMN last_stall_alert_at TIMESTAMPTZ;
    ```
  - Covered by `tests/test_tasks_detect_stalled_jobs.py`: a stalled
    `QUEUED` job triggers one alert and sets `last_stall_alert_at`; a
    fresh job doesn't alert; a job already alerted within the re-alert
    window doesn't alert again; a job whose last alert is older than the
    re-alert window alerts again; a `PUBLISHED` job is never considered
    stalled regardless of `updated_at` age.

### Phase 15 (current)
- **`app/media_probe.py`** (new) — `probe(path) -> {"duration_seconds":
  float, "width": int, "height": int}`, a pure subprocess wrapper around
  `ffprobe` (`ffprobe -v error -select_streams v:0 -show_entries
  stream=width,height:format=duration -of json`). Raises `PermanentError`
  (never `TransientError`, matching the publisher exceptions' semantics —
  see `app/exceptions.py`) if `ffprobe` isn't on `PATH`, the process fails
  to start or times out (30s), exits non-zero, or its output can't be
  parsed (e.g. a file with no video stream). Written to be reused by any
  publisher that needs a video's shape before uploading — `youtube.py` is
  the first caller (below); `tiktok.py` is a natural next candidate if it
  ever needs its own duration/aspect-ratio constraints.
- **YouTube Shorts validation** (`app/publishers/youtube.py`) — new
  optional payload key `"shorts"` (bool, default `False`), backwards
  compatible. When `True`, the video is probed with `media_probe.probe()`
  *before* upload (no HTTP call made if validation fails): duration over
  60s, or a non-vertical aspect ratio (`height <= width`), raises
  `PermanentError` naming the actual duration/dimensions. When absent or
  `False`, no probing happens at all — behavior is unchanged from before
  this phase.
- **YouTube playlist assignment** (`app/publishers/youtube.py`) — new
  optional payload key `"playlist_id"`. After a successful
  `videos.insert`, `_assign_to_playlist` calls `playlistItems.insert` to
  add the video. **Deliberate failure semantics**: the video is already
  live on YouTube by that point, so a playlist failure must never fail the
  job or trigger a retry — `publish_job`'s retry path would re-upload the
  video, which is worse than just not being in a playlist. The playlist
  call is wrapped in its own `try/except Exception` *inside* `publish()`,
  so it can never reach the outer `except HttpError`/`except Exception`
  blocks that classify and raise; instead it's logged as a warning and
  surfaced as `result["playlist_error"]`, while `result["external_id"]`
  still reports the successful upload.
- **Scope change**: `playlistItems.insert` needs the broader
  `https://www.googleapis.com/auth/youtube` scope — `youtube.upload` alone
  (the only scope requested through Phase 14) isn't enough. `SCOPES` in
  `youtube.py` now requests both, shared as always with
  `scripts/authorize_youtube.py`. **Any `Account` (or single-account
  `token.json`) authorized before this phase only has `youtube.upload`
  and must be re-authorized** (`python -m scripts.authorize_youtube
  [--account NAME]`) before `playlist_id` will work against it — until
  then, `playlist_id` jobs for that account/token will upload
  successfully but always land in `result["playlist_error"]` with an
  insufficient-scope error.
- **Dashboard NEW JOB form** (`dashboard/`, not `app/`) — when
  `platform=youtube`, three new optional fields: a privacy select
  (private/unlisted/public, default private), a "Shorts" checkbox, and a
  playlist ID text input. `dashboard/api.py::create_job` gained matching
  `Form(...)` parameters (`privacy`, `shorts`, `playlist_id`) and passes
  them straight into the job payload for youtube jobs — no validation of
  its own beyond what the upload flow already does; `youtube.py` owns all
  the actual Shorts/playlist validation, same separation as every other
  publisher-specific payload field.
- Covered by `tests/test_media_probe.py` (ffprobe missing, non-zero exit,
  malformed JSON, no video stream, subprocess timeout — `subprocess.run`
  and `shutil.which` mocked, no real `ffprobe` invoked) and
  `tests/test_publisher_youtube.py` (61s video rejected with no upload
  call made; horizontal video rejected; valid 30s vertical video uploads;
  `shorts` absent skips probing entirely — asserted by making `probe()`
  raise if called; playlist success adds the item; playlist failure still
  returns `external_id` with `playlist_error` set and doesn't raise). The
  YouTube API itself is mocked by replacing `build()` with an in-memory
  fake service object (`googleapiclient` talks `httplib2`, not `requests`,
  so `responses` — used for tiktok.py/twitter.py — doesn't apply here);
  credential loading is mocked by replacing `_load_credentials` directly,
  same spirit as the rest of the suite avoiding real OAuth/network calls.

### Phase 17 (current)
- **X (Twitter) media attachments + thread chaining** — the two biggest
  spec gaps against `app/publishers/twitter.py`. Still no real X
  credentials (client is creating the Developer Portal account), so
  everything is unit-tested against fully mocked HTTP
  (`tests/test_publisher_twitter_media.py`), same as the rest of the
  publisher. The pure-publisher contract (no Celery/DB imports, typed
  `TransientError`/`PermanentError`) and Phase 6's multi-account
  credential resolution are unchanged — `_resolve_credentials` now returns
  a plain dict (`api_key`/`api_secret`/`access_token`/`access_token_secret`)
  instead of building a `tweepy.Client` directly, since Phase 17 needs to
  build *two* tweepy objects from the same resolved credentials: `Client`
  (v2, tweet creation) and `API`/`OAuth1UserHandler` (v1.1, media upload —
  media upload lives on a different API version/host,
  `upload.twitter.com`, than tweet creation).
  - **Chunked media upload** (`_upload_media`/`_upload_one_media`): INIT ->
    APPEND (4 MiB chunks) -> FINALIZE against
    `upload.twitter.com/1.1/media/upload.json`, OAuth 1.0a signed. Built on
    `tweepy.API`'s already-correct low-level methods
    (`chunked_upload_init`/`_append`/`_finalize`/`get_media_upload_status`)
    reused purely for their OAuth signing and endpoint plumbing —
    deliberately *not* tweepy's own higher-level `chunked_upload()`/
    `media_upload()` combinators, since this module needs to drive the
    processing-status polling itself
    (`processing_info.state`: pending/in_progress/succeeded/failed) so a
    failed async video/gif processing step raises `PermanentError` with
    the reason, the same way every other error here is reported. Static
    images finalize synchronously (no `processing_info`), so no STATUS
    polling happens for them. Errors classified via the same
    `_classify_http_error` used for tweet creation (429/5xx ->
    `TransientError`; 401/403/other 4xx -> `PermanentError`) — it's generic
    over any tweepy `HTTPException`, v1.1 or v2.
  - **Payload contract, backwards compatible**:
    - `"text"` alone -> single tweet, unchanged.
    - optional `"media_paths"`: list of local file paths, uploaded then
      attached via `media_ids`. X caps (validated pre-flight, before any
      upload starts): 4 images or 1 video per tweet, never mixed.
    - optional `"thread"`: ordered list of `{"text", "media_paths"?}`
      dicts, posted sequentially, each reply chained to the previous via
      `in_reply_to_tweet_id`. **Every tweet's text (280-char guard, Phase
      13) and media caps are validated up front, across the whole thread,
      before tweet #1 is posted** (`_validate_thread`) — a validation error
      we could have caught never leaves a half-posted thread.
    - `"text"` and `"thread"` are mutually exclusive:
      `_validate_top_level_payload` raises `PermanentError` if both or
      neither are present.
    - Result dict: `external_id` is the first tweet's id; threads
      additionally return `"tweet_ids"` (every tweet's id, in order).
  - **Partial-thread failure reporting**: if a mid-thread API call fails
    (tweet creation or media upload), `_publish_thread` catches the typed
    error per-tweet and re-raises the *same* error class with an augmented
    message stating how many tweets were already posted and the last
    successful tweet id — this is what ends up in `Job.error_message`/the
    DLQ alert, so a partial thread is diagnosable instead of just "some
    tweet failed."
  - Covered by `tests/test_publisher_twitter_media.py`: media cap
    validation (5 images, 2 videos, mixed image+video, missing file — all
    rejected with zero HTTP calls made); chunked upload happy path for
    video (INIT/APPEND x2/FINALIZE/STATUS poll to `succeeded`) and image
    (no STATUS poll); failed processing -> `PermanentError` with the
    reason and no tweet posted; thread happy path asserting the actual
    `in_reply_to_tweet_id` chaining in each request body; an over-280-char
    tweet anywhere in a thread posts nothing; mid-thread failure reports
    the posted count and last tweet id.

### Phase 20 (current)
- **Timezone-aware scheduling** (`app/config.py`) — fixes Phase 5's
  time-slot scheduling, which interpreted `PLATFORM_TIME_SLOTS`/`TIME_SLOTS`
  times in whatever timezone the server process happened to run in
  (harmless on a developer's Mac, silently wrong the moment this runs on
  Fly, which is UTC).
  - **`SCHEDULER_TIMEZONE`** (env var, IANA name e.g.
    `"America/Argentina/Buenos_Aires"`, default `"UTC"`) — parsed with
    `zoneinfo.ZoneInfo` at import time into the module-level
    `SCHEDULER_TIMEZONE` constant. An invalid name raises `RuntimeError`
    immediately at startup (same fail-fast pattern as `Settings.__post_init__`
    for a missing `DATABASE_URL`), rather than failing later, confusingly,
    the first time a job gets scheduled.
  - **Slot semantics**: `PLATFORM_TIME_SLOTS`/`TIME_SLOTS` times (e.g.
    `"09:00"`) are the business's wall-clock intent and are now interpreted
    in `SCHEDULER_TIMEZONE`, not the server's local timezone. `next_slot_for`
    converts `now` to `SCHEDULER_TIMEZONE` to decide which slot is next
    (including which local *date* "today" is — important right around UTC
    midnight, see the crossing-midnight test below), then converts the
    chosen slot back to aware UTC before returning it. Everything stored
    and compared elsewhere (the `scheduled_at` column, `dispatch_due_jobs`)
    stays in UTC — `SCHEDULER_TIMEZONE` only affects how slot times are
    *interpreted*, never what's persisted.
  - **DST**: handled by `zoneinfo` (construct the aware local datetime
    directly via `datetime.combine(..., tzinfo=SCHEDULER_TIMEZONE)`, then
    `.astimezone(timezone.utc)`) — not specially guarded. During a DST
    transition in `SCHEDULER_TIMEZONE`, a slot can shift by an hour or,
    for a slot time that falls in a skipped/repeated local hour, resolve
    per Python's normal PEP 495 fold/gap rules — the same way any other
    wall-clock-based schedule behaves across a DST boundary. Accepted, not
    worked around.
  - **`dispatch_due_jobs`** (`app/tasks.py`) now compares against
    `datetime.now(timezone.utc)` instead of naive `datetime.now()` — the
    bug this phase actually fixes for the Beat task, since comparing a
    naive-local `now` against an aware-UTC `scheduled_at` was silently
    wrong on any server not in UTC (i.e., would have been wrong on Fly from
    day one). `scripts/enqueue_demo.py --schedule` was calling
    `next_slot_for` with naive local `datetime.now()` too; also switched to
    aware `datetime.now(timezone.utc)`.
  - **Backwards compatible, no schema change**: `scheduled_at` was already
    `DateTime(timezone=True)` (Phase 5) and existing rows are already
    UTC-ish. Comparisons/reads that need Python-side tzinfo normalize a
    naive value as already-UTC via `_ensure_utc` — duplicated in
    `app/config.py` (rather than imported from `app/tasks.py`) to avoid a
    config <-> tasks import cycle, same normalization already used by
    `app/tasks.py::_ensure_utc` and
    `app/publishers/youtube.py::token_expires_within`. SQLite (the test
    suite's DB) round-trips *every* datetime as naive regardless of what
    was written, ignoring tzinfo entirely on both read and write — verified
    directly against `sqlalchemy.dialects.sqlite.base.DATETIME`'s bind
    processor — which is exactly why `dispatch_due_jobs`'s SQL-side
    `scheduled_at <= now` filter works correctly against a mix of aware and
    naive stored values as long as every value's raw field numbers already
    represent the same UTC instant (Postgres, the real target, normalizes
    properly regardless via `TIMESTAMPTZ`).
  - Covered by `tests/test_config_scheduler_timezone.py` (slot conversion
    for a non-UTC `SCHEDULER_TIMEZONE`, including a case where `now`'s UTC
    date and its local date disagree, asserting the local date is what's
    used; default-UTC behavior unchanged; naive `now` treated as UTC; an
    invalid timezone name fails at import via a subprocess, since the
    failure happens at module import time and the main test process
    already has `app.config` loaded) and
    `tests/test_tasks_dispatch_due_jobs.py` (`dispatch_due_jobs` picks up a
    due job whether `scheduled_at` is aware or naive, and correctly leaves
    a future-dated job alone), `publish_job.delay` monkeypatched so no
    Celery broker is needed.

### Phase 21 (current)
- **Twitter/X migrated to OAuth 2.0 + API v2 media upload** — X's developer
  console now only issues OAuth 2.0 user tokens (scopes: `tweet.read`,
  `tweet.write`, `users.read`, `offline.access`), replacing Phase 6/17's
  OAuth 1.0a + v1.1 media upload entirely. No real X credentials existed
  when Phase 6/17 were built either, so — same as every publisher in this
  project before real credentials arrive — this is unit-tested against
  fully mocked HTTP only (`tests/test_publisher_twitter.py`,
  `tests/test_publisher_twitter_media.py`,
  `tests/test_tasks_twitter_token_refresh.py`), not yet verified against a
  live account.
  - **Auth**: every API call is now a plain `Authorization: Bearer
    <access_token>` request — no per-request signing like OAuth 1.0a.
    `tweepy.Client(bearer_token=...)` handles tweet creation, but tweepy's
    write methods default to `user_auth=True` (OAuth 1.0a) regardless of
    whether a `bearer_token` was given, so `_post_tweet` explicitly passes
    `user_auth=False` to actually route through it — an easy trap, called
    out inline in the code.
  - **Credentials, all four now per-account**: `account_credentials` (or
    the env fallback) carries `client_id`, `client_secret`, `access_token`,
    `refresh_token` — a change from Phase 6's split (app-level env vars +
    per-account token), because a confidential client's refresh flow needs
    the client id/secret alongside whichever refresh_token they're paired
    with. Env fallback (single-account mode, no `account_id`):
    `TWITTER_CLIENT_ID` / `TWITTER_CLIENT_SECRET` / `TWITTER_ACCESS_TOKEN` /
    `TWITTER_REFRESH_TOKEN`. **Single-account mode can't persist a rotated
    refresh_token anywhere** (see rotation below) — it survives exactly one
    reactive refresh before the env var goes stale, so real accounts should
    get an `Account` row via `scripts/add_account.py` rather than relying
    on env vars long-term.
  - **Media upload migrated to X API v2**, a different host than tweet
    creation (`api.x.com`, not `api.twitter.com`), Bearer auth, same
    `media_category` values (`tweet_image`/`tweet_gif`/`tweet_video`), the
    4-images-or-1-video-never-mixed pre-flight cap, and attaching the
    uploaded `media_id` to a tweet via `create_tweet(media_ids=[...])`, all
    unchanged from Phase 17 — X's v2 tweet body nests this as `{"media":
    {"media_ids": [...]}}`, which is what tweepy already sent regardless of
    auth scheme. Implemented directly with `requests` rather than tweepy
    (tweepy predates this v2 endpoint).
    - **Endpoint shape corrected 2026-09-09** (this session, discovered via
      a real 400 `"Missing media field in JSON"` from a live account — the
      first real X API traffic this project has sent). Phase 21 originally
      built this as a single endpoint,
      `https://api.x.com/2/media/upload`, with a
      `command=INIT|APPEND|FINALIZE` form field (mirroring the v1.1 shape
      from Phase 17), because that's what was assumed by analogy to v1.1
      rather than read off docs.x.com for v2. The real v2 shape (confirmed
      against current docs.x.com) is **three separate RESTful endpoints**:
      - `POST /2/media/upload/initialize` — JSON body
        (`{"media_type", "total_bytes", "media_category"}`), not
        multipart/form-data.
      - `POST /2/media/upload/{media_id}/append` — multipart/form-data
        (`segment_index` + the binary `media` field), but `media_id` is now
        in the URL path, not a form field.
      - `POST /2/media/upload/{media_id}/finalize` — empty body, `media_id`
        in the URL path.
      **STATUS did NOT move** — it's still `GET
      /2/media/upload?command=STATUS&media_id=...`, `media_id` as a query
      param, not a path segment — verified independently rather than
      assumed to have moved just because the other three did.
      `_media_init`/`_media_append`/`_media_finalize`/`_media_status` in
      `app/publishers/twitter.py` (and the base URL constant, renamed
      `_MEDIA_UPLOAD_BASE_URL`) were updated to match; the endpoint-shape
      mocks in `tests/test_publisher_twitter_media.py` (`_mock_media_upload`)
      were updated to register the new per-step URLs, plus a new
      `TestChunkedMediaUpload::test_request_shapes_match_current_docs_x_com`
      asserting the JSON body on initialize, the path-based `media_id` on
      append/finalize, and the query-param `media_id` on STATUS, so a
      future regression back to the wrong shape fails a test instead of
      only surfacing against a live account again. This bug was invisible
      to the mocked test suite the whole time Phase 21 stood — the mocks
      matched the (wrong) shape the code assumed, so tests were internally
      consistent but wrong about the real API; this is the first time any
      of `twitter.py`'s untested-against-a-live-account caveats (see the
      module docstring) actually got exercised against X's real servers.
  - **`TokenExpiredError`** (`app/exceptions.py`) — a new `TransientError`
    subclass. `twitter.py` raises it instead of a plain `PermanentError`
    when X answers 401 with `WWW-Authenticate: Bearer error="invalid_token"`
    (RFC 6750) — this specific signal, not yet verified against a live
    response, is what distinguishes "token needs a refresh" from "these
    credentials are just wrong." Being a `TransientError` subclass means
    any code path that doesn't specifically check for it still treats it as
    an ordinary retryable error.
  - **Token refresh, modeled on `youtube.py`/`tiktok.py`'s pattern but with
    rotation**: `token_expires_within(credentials, seconds)` and
    `refresh_stored_credentials(credentials)` in `twitter.py` mirror the
    other publishers' contract (same `"expires_at"` key/ISO-string
    convention as `tiktok.py`). The refresh call is
    `POST https://api.twitter.com/2/oauth2/token` (`grant_type=refresh_token`),
    authenticated as the confidential client via HTTP Basic auth
    (`client_id:client_secret`) — this exact token endpoint URL, unlike the
    v2 media upload endpoint above, was not pasted from docs.x.com and is
    flagged in-code as unverified. **X's refresh tokens are single-use and
    ROTATE**: every refresh returns a new `access_token` AND a new
    `refresh_token`, invalidating the old `refresh_token` — a response
    missing the new `refresh_token` is treated as a `TransientError` rather
    than silently reusing the now-invalid old one.
  - **Registered in `app/tasks.py::_TOKEN_REFRESH_MODULES_BY_PLATFORM`**
    (alongside youtube/tiktok), so the existing `refresh_expiring_tokens`
    Beat task (every 30 min, Phase 8) now also proactively refreshes
    Twitter accounts. Refresh window is **platform-specific**
    (`_TOKEN_REFRESH_WINDOW_SECONDS_BY_PLATFORM`): 40 minutes for twitter
    (access tokens live ~2h) vs. the 45-minute default for youtube/tiktok.
  - **Reactive refresh-and-retry-once in `publish_job`**
    (`app/tasks.py::_handle_token_expired`): when a publisher raises
    `TokenExpiredError`, `publish_job` refreshes the account's stored
    credentials, **persists the rotated access+refresh tokens to the
    `Account` row before reusing them** (losing a rotated refresh_token
    here would strand the account exactly like never refreshing at all),
    then retries the publish call once with the fresh credentials — inline,
    not via `self.retry()`, so it doesn't consume one of `max_retries`.
    Whatever that retry raises (success, another `TokenExpiredError`, a
    plain `TransientError`, or `PermanentError`) is handled exactly like a
    first attempt by the surrounding retry/DLQ logic. A job with no
    `account_id` can't be refreshed (nowhere to persist a rotated
    refresh_token) — it falls through to the original `TokenExpiredError`
    being retried as an ordinary transient error instead.
  - **Deactivation + alert on a permanently invalid refresh token**: shared
    between the proactive Beat path and the reactive retry path via
    `_deactivate_and_alert_for_reauth` — sets the `Account` `is_active =
    False` (so `_resolve_account_credentials` stops routing jobs to it) and
    alerts with platform-specific re-authorization instructions
    (`_REAUTHORIZE_INSTRUCTIONS_BY_PLATFORM`). Twitter has no interactive
    authorize script yet (unlike `scripts/authorize_youtube.py`/
    `authorize_tiktok.py`) — the alert points at obtaining a fresh
    authorization code via X's OAuth 2.0 PKCE flow by hand and registering
    it with `scripts/add_account.py`.
- `scripts/add_account.py` — unchanged mechanically (still generic
  key=value pairs), docstring updated with a Twitter OAuth 2.0 example:
  `client_id=... client_secret=... access_token=... refresh_token=...
  expires_at=...`.
- `scripts/enqueue_twitter_test.py` (new, modeled on
  `enqueue_youtube_test.py`) — `--mode text|image|video|thread` builds the
  matching payload shape (thread mode posts 3 generated tweets, with
  `--file` attaching to the first one if given), `--file` for image/video
  modes, `--account NAME` to link an `Account` row (omit for the env-var
  single-account fallback). Dispatches immediately via the normal queue,
  same as the youtube script.
- **Dashboard NEW JOB form**: initially shipped with Twitter absent (it
  wasn't in `dashboard/api.py`'s upload-platforms set, and a single
  tweet/thread doesn't fit a upload-a-file UI shape the way youtube/tiktok
  do) — added afterward once it was noticed the form silently only offered
  youtube/tiktok despite twitter accounts existing and working fine via
  scripts. `_SUPPORTED_UPLOAD_PLATFORMS` split into `_SUPPORTED_PLATFORMS`
  (all three) and `_VIDEO_UPLOAD_PLATFORMS` (youtube/tiktok only, the ones
  that require a single video `file`); `create_job` now also accepts an
  optional `text` field and an optional `media_files` list (0 or more).
  Twitter branch builds `{"text": ..., "media_paths": [...]}` — same
  "publisher owns validation" split as every other platform here:
  `app/publishers/twitter.py` still owns the 4-images-or-1-video cap and
  file-type checks, the route doesn't duplicate them. No file is required
  for twitter; account selection reuses the existing
  "no account (single-account fallback)" placeholder since twitter, like
  youtube, supports the env-var fallback. Threads are intentionally **not**
  exposed through this form — script-only
  (`scripts/enqueue_twitter_test.py --mode thread`).

  **When real X OAuth 2.0 credentials arrive**, replacing the old OAuth
  1.0a `X_API_KEY`/`X_API_SECRET`/`X_ACCESS_TOKEN`/`X_ACCESS_TOKEN_SECRET`
  vars entirely:
  1. Set `TWITTER_CLIENT_ID`/`TWITTER_CLIENT_SECRET` in `.env` for
     single-account use, plus `TWITTER_ACCESS_TOKEN`/`TWITTER_REFRESH_TOKEN`
     from the initial OAuth 2.0 PKCE authorization — or register an
     `Account` row via `scripts/add_account.py` (recommended: only an
     `Account` row survives refresh-token rotation).
  2. Test with `python -m scripts.enqueue_twitter_test --mode text
     [--account NAME]`, then image/video/thread modes once that works.
  3. Watch the first proactive refresh (`refresh_expiring_tokens`, every
     30 min) or trigger one manually to confirm rotation persists
     correctly onto the `Account` row (`scripts/show_accounts.py`).

### Phase 22 (current)
- **R2 wired to a real bucket** (`app/storage.py`) — bucket
  `arscor-distribution-media` now exists. New `upload_file(local_path) ->
  {"key": ..., "public_url": ...}` alongside the existing
  `upload_media`/`generate_signed_url`/`delete_media` (unchanged): the key
  is namespaced by UTC date + a random uuid segment
  (`YYYY/MM/DD/<uuid>.<ext>`, `_build_key`) to avoid collisions, and
  `public_url` is `R2_PUBLIC_BASE_URL + "/" + key` — a plain public URL
  (not presigned), for callers that need the bucket's own public r2.dev
  address rather than a time-limited signed GET. New setting
  `r2_public_base_url` (`app/config.py`, env `R2_PUBLIC_BASE_URL`), same
  optional-at-the-Settings-layer treatment as the other `R2_*` vars —
  `upload_file` raises `StorageNotConfiguredError` naming exactly which of
  `R2_ENDPOINT_URL`/`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY`/
  `R2_BUCKET_NAME`/`R2_PUBLIC_BASE_URL` is missing, same pattern as
  `_require_config` already used.
- **Wired into the dashboard's NEW JOB upload flow** (`dashboard/api.py`) —
  every file `_save_upload` writes to local disk is now also best-effort
  staged to R2 via a new `_stage_to_r2(local_path) -> str | None` helper,
  called right after each `_save_upload`. **Never raises**: it catches
  `StorageNotConfiguredError` and any other exception (logged), returning
  `None` either way — R2 being unconfigured or unreachable must never break
  job creation, so `create_job` degrades to exactly today's behavior (local
  path only, no crash) whenever staging doesn't succeed. The local path in
  the payload is unchanged and stays what every publisher actually reads;
  when staging succeeds, a public URL is attached alongside it:
  - youtube/tiktok (single video file): `payload["media_public_url"]`.
  - facebook (single optional media file): `payload["media_public_url"]`.
  - twitter (`media_files`, 0+): `payload["media_public_urls"]`, an
    all-or-nothing list aligned index-for-index with `media_paths` — if
    staging fails for even one file, the whole key is omitted rather than
    attached as a partial/misaligned list a future consumer could
    misinterpret.
  This groundwork exists for the upcoming Instagram publisher (the
  Instagram Content Publishing API needs a publicly-fetchable media URL,
  not a local path — unlike every publisher built so far) and for the Fly
  deploy (Phase 9's single-machine-mode note: once media isn't confined to
  one process's local disk, `worker`/`beat`/`api` can split into separate
  Fly processes/machines again). **No publisher reads `media_public_url(s)`
  yet** — it's stored on the job payload only; wiring an actual Instagram
  publisher to consume it is a future phase.
- **Local dev without R2 access stays fully usable**: every `R2_*` env var
  can be empty and nothing crashes anywhere in the pipeline — confirmed by
  `tests/test_dashboard_media_staging.py`'s graceful-degradation tests
  (`StorageNotConfiguredError` and an arbitrary unexpected exception both
  result in a normally-created job with no `media_public_url(s)` key).
- `scripts/r2_smoke_test.py` (new) — uploads a tiny generated file via
  `upload_file`, prints its key and public URL (fetchable directly in a
  browser to confirm the bucket is actually public), then deletes it.
  Complements the existing `scripts/test_storage.py` (which exercises
  `upload_media`/`generate_signed_url`/`delete_media` directly instead).
- Covered by `tests/test_storage.py` (`upload_file` happy path, key
  format/uniqueness, each missing `R2_*` var raising
  `StorageNotConfiguredError` by name, `boto3.client` never called when
  unconfigured; `upload_media`/`generate_signed_url`/`delete_media`
  unchanged-behavior checks) and `tests/test_dashboard_media_staging.py`
  (`_stage_to_r2`'s three outcomes in isolation; `create_job` end-to-end
  for youtube attaching `media_public_url`, both graceful-degradation
  paths, and twitter's all-succeed vs. partial-failure
  `media_public_urls` behavior — `create_job` is called directly as a
  plain async function with a real `UploadFile`, bypassing the HTTP layer,
  same spirit as the rest of the suite calling task functions directly
  instead of going through Celery/a live server).

  **Still missing from `.env`**: `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY`, and `R2_PUBLIC_BASE_URL` (only `R2_BUCKET_NAME`
  is set so far) — `python -m scripts.r2_smoke_test` reports exactly this
  and is the fastest way to confirm the bucket end-to-end once they're
  filled in.

### Phase 23 (current)
- **Meta (Facebook + Instagram) OAuth foundation** — built "ready waiting for
  credentials," same posture as the Twitter publisher before real X
  Developer Portal credentials existed: `META_APP_ID`/`META_APP_SECRET`
  don't exist yet, nothing reads them at import time, every code path that
  needs them raises a clear `PermanentError` instead of crashing, and the
  whole thing is exercised only against fully mocked HTTP
  (`tests/test_publisher_meta.py`, `tests/test_authorize_meta.py`,
  `tests/test_tasks_meta_token_refresh.py`).
  - **Scope is OAuth mechanics only — no publish() yet.** `app/publishers/meta.py`
    has no `publish()` function, and `app/tasks.py::_PUBLISHERS_BY_PLATFORM`
    has no `"facebook"`/`"instagram"` entry — jobs on those platforms
    currently fall back to the fake publisher, same as any platform without
    a real integration. Building the actual content-posting flow (photo/
    video upload to a Page, the two-step IG container/publish flow) is a
    natural next phase once this foundation is exercised against a real
    Meta App.
  - **Graph API version**: `v26.0` (current at the time this was built —
    Meta deprecates versions on a schedule; this will need bumping
    eventually, but as a deliberate code change, not an env-configurable
    knob). `GRAPH_API_BASE`/`AUTHORIZE_URL` constants in
    `app/publishers/meta.py`.
  - **OAuth chain** (`app/publishers/meta.py`, endpoint shapes given
    directly per the phase brief, not guessed):
    1. Browser dialog (`scripts/authorize_meta.py` opens this):
       `GET /v26.0/dialog/oauth?client_id&redirect_uri&state&scope` ->
       redirects back with `?code=...&state=...`.
    2. `exchange_code_for_user_token(code, redirect_uri)` — code -> a
       short-lived user token.
    3. `exchange_long_lived_token(short_lived_token)` — short-lived -> a
       long-lived (~60 day) user token. **This is also how a long-lived
       token gets refreshed later** — Meta has no separate rotating
       refresh_token the way Twitter does, or a distinct refresh endpoint
       like TikTok's/YouTube's; you just re-exchange the current token
       before it expires.
    4. `list_pages(user_token)` — `GET /me/accounts` -> every Page the user
       manages plus a Page-scoped access token for each. Page tokens minted
       from a long-lived user token are documented to not expire in
       practice — **not independently verified against a live token yet**,
       flagged in-code rather than asserted as fact.
    5. `get_instagram_business_account(page_id, page_token)` — `GET
       /<page_id>?fields=instagram_business_account` -> the linked IG
       Business account's id, or `None` (not an error) if the Page has none
       linked.
  - **Credential shapes** (`Account.credentials`, both created by
    `scripts/authorize_meta.py`):
    - platform `"facebook"`: `{page_id, page_token, page_name, user_token,
      user_token_expires_at}`.
    - platform `"instagram"`: `{ig_user_id, page_id, page_token, user_token,
      user_token_expires_at}`.
    Both carry `user_token` + `page_id`, which is all
    `refresh_stored_credentials()` needs — it works unchanged for either
    platform's Account row.
  - **Graph API error classification** (`_raise_for_graph_error`): unlike
    TikTok's Content Posting API, Graph API always pairs an
    `{"error": {...}}` body with a non-2xx HTTP status (never reports
    errors as HTTP 200). HTTP 429/5xx, or `error.code` in `{1, 2, 4, 17, 32,
    613}` (Meta's documented API/rate-limit codes) -> `TransientError`;
    `code=190` (`OAuthException` — invalid/expired/revoked token) and every
    other 4xx -> `PermanentError`. Not independently verified against live
    responses yet, same caveat as every other publisher's error-code table
    in this package before real credentials exist.
  - **Proactive token refresh, registered in `app/tasks.py`**:
    `_TOKEN_REFRESH_MODULES_BY_PLATFORM` gets `"facebook"` and
    `"instagram"` entries (both pointing at `app/publishers/meta.py`, same
    module for either platform) so the existing `refresh_expiring_tokens`
    Beat task (every 30 min, Phase 8) also manages Meta accounts. Refresh
    window is **7 days** before expiry
    (`_TOKEN_REFRESH_WINDOW_SECONDS_BY_PLATFORM["facebook"/"instagram"]`) —
    far wider than the 45-minute default, since Meta's long-lived tokens
    last ~60 days. On refresh, `refresh_stored_credentials` re-exchanges the
    stored `user_token` and re-fetches the Page token for `page_id` (found
    by matching `page_id` in a fresh `list_pages()` call); a `PermanentError`
    (invalid token, or the Page no longer accessible) deactivates the
    Account and alerts via the same `_deactivate_and_alert_for_reauth`
    helper Twitter/TikTok/YouTube use, pointing at
    `scripts.authorize_meta` in `_REAUTHORIZE_INSTRUCTIONS_BY_PLATFORM`.
    **No reactive refresh-on-error path** (unlike Twitter's
    `TokenExpiredError` handling in `publish_job`) — there's no `publish()`
    to ever raise it yet.
  - **Meta tokens don't rotate single-use like Twitter's**: the same
    long-lived `user_token` keeps working to mint new ones via
    `exchange_long_lived_token`, so — unlike `twitter.py` — there's no
    "lost the newly-rotated token, stranded the account" risk from calling
    `refresh_stored_credentials` more than once against the same starting
    credentials.
- `scripts/authorize_meta.py` — interactive OAuth, modeled on
  `scripts/authorize_tiktok.py`'s local-redirect-listener + state-check
  shape: opens a browser for the Facebook Login dialog, waits for the
  callback, runs the full exchange chain above, lists the user's Pages
  (prompting the operator to choose one if there's more than one via
  `_choose_page`), looks up the linked Instagram Business account, and
  upserts Account row(s) via the same `upsert_account` helper as every
  other authorize script. If the chosen Page has no linked Instagram
  Business account, only the `facebook` Account is created and a warning is
  printed explaining the Business-account + Page-link requirement (convert
  to Business/Creator, link it to the Page) — nothing crashes, the flow
  just stops one Account row short. `--account NAME` overrides the Account
  name (default: the Page's own name) for cases where multiple client Pages
  share a display name; re-running with the same resulting name rotates
  that Account's credentials in place. The chain after a valid callback is
  split into `_run_authorization(code, redirect_uri, account_name)`
  specifically so it's testable without a real browser or local HTTP
  server — `tests/test_authorize_meta.py` monkeypatches the four OAuth-chain
  functions and calls it directly.
  - **Redirect URI / localhost caveat — UNVERIFIED**: unlike TikTok's
    Developer Portal (which rejects localhost/127.0.0.1 outright, requiring
    the public-forwarder-page trick from Phase 10), Meta's App Dashboard is
    *documented* to allow `http://localhost` redirect URIs for an app still
    in Development mode. This script assumes that and binds its one-shot
    callback server directly to `META_REDIRECT_URI`'s host:port — no
    forwarder page. **If the real Dashboard rejects a localhost URI once
    `META_APP_ID` exists, this needs the same forwarder-page trick as
    TikTok** — treat that as a follow-up, not a sign the script is broken.

  **When Meta credentials arrive:**
  1. Create/select an app at developers.facebook.com/apps, add the Facebook
     Login product, and register a redirect URI matching
     `META_REDIRECT_URI` (see the localhost caveat above — may need the
     TikTok-style forwarder page instead).
  2. Request the `pages_manage_posts`, `pages_show_list`,
     `pages_read_engagement`, `instagram_basic`, `instagram_content_publish`,
     `business_management` permissions — most need App Review before they
     work for anyone other than the app's own admins/developers/testers.
  3. Set `META_APP_ID`, `META_APP_SECRET`, `META_REDIRECT_URI` in `.env`.
  4. Run `python -m scripts.authorize_meta [--account NAME]`. For Instagram
     publishing, make sure the target IG account is Business/Creator and
     linked to the Facebook Page *before* running this — otherwise only the
     `facebook` Account gets created (re-run after linking to add the
     `instagram` one).
  5. Confirm with `scripts/show_accounts.py`, then watch a proactive refresh
     (`refresh_expiring_tokens`, every 30 min, 7-day window) or trigger one
     manually to confirm re-exchange persists correctly onto the Account
     rows.
  6. Building the actual publish flow (photo/video upload, IG container/
     publish) is the natural next phase — not built yet, see "Scope" above.

### Phase 24 (current)
- **Facebook Pages publisher** (`app/publishers/facebook.py`) — the first
  real `publish()` built on Phase 23's Meta OAuth foundation, built "ready
  waiting for credentials" the same way (no real Meta App exists yet, so
  everything is exercised only against fully mocked HTTP —
  `tests/test_publisher_facebook.py`,
  `tests/test_tasks_facebook_token_refresh.py`). Wired into
  `app/tasks.py::_PUBLISHERS_BY_PLATFORM["facebook"]`; `graph-video.facebook.com`
  is deprecated (per current developers.facebook.com docs) — every call goes
  through `graph.facebook.com` (`meta.py`'s existing `GRAPH_API_BASE`,
  v26.0).
  - **No env-var single-account fallback**, per Phase 23's decision:
    `_resolve_credentials` requires `account_credentials` (an `Account` row,
    `page_id` + `page_token`, the shape `scripts/authorize_meta.py`
    creates) and raises `PermanentError` if the job has no `account_id`.
  - **Three payload shapes, auto-detected**: `{"text"}` alone posts to
    `POST /<page_id>/feed`. `{"text"?, "media_paths": [one path]}` posts a
    photo (`POST /<page_id>/photos`, multipart `source`) or a video
    (resumable upload, below) depending on the single file's guessed MIME
    type — no separate "kind" flag, same spirit as
    `twitter.py`'s `_media_kind`. `text` is the caption for a photo, the
    description for a video; both are optional when media is attached
    (unlike Twitter, a caption-less media post is valid) — enforced by
    `_validate_top_level_payload` (at least one of `text`/`media_paths`
    required) rather than requiring `text` unconditionally.
  - **Resumable video upload** (3 Graph API calls, endpoint shapes given
    directly in the phase brief, not guessed):
    1. `_start_upload_session`: `POST /<APP_ID>/uploads?file_name&
       file_length&file_type&access_token=<PAGE_TOKEN>` ->
       `{"id": "upload:<SESSION_ID>"}`. `APP_ID` comes from env
       `META_APP_ID` (app-level, not per-account) — the only place this
       module reads an env var directly, mirroring `meta.py`'s
       `_app_credentials()`.
    2. `_upload_video_binary`: `POST /upload:<SESSION_ID>`, header
       `Authorization: OAuth <PAGE_TOKEN>` + `file_offset: <n>`, raw binary
       body -> `{"h": "<FILE_HANDLE>"}`. **Interruption/resume**: if this
       fails partway (a network error, or a transient Graph error),
       `_get_upload_offset` (`GET /upload:<SESSION_ID>`, same `OAuth`
       header) asks how many bytes the server actually received, and the
       POST is retried starting from that byte instead of restarting the
       whole file, up to `_MAX_UPLOAD_ATTEMPTS` (3) attempts total.
       **`TokenExpiredError` is deliberately excluded from this retry
       loop** (it's a `TransientError` subclass, so it would otherwise be
       caught by the same `except` clause as an ordinary network blip) — it
       must propagate to `app/tasks.py` unrelabeled so the reactive-refresh
       path below actually runs, not get repackaged as a generic
       `TransientError` after exhausting upload retries.
    3. `_publish_video`: `POST /<page_id>/videos`,
       `fbuploader_video_file_chunk=<FILE_HANDLE>` + `title`/`description`
       + `access_token=<PAGE_TOKEN>` -> `{"id": "<video_id>"}` — the video
       only actually goes live at this step; steps 1-2 alone publish
       nothing.
    Access token for all three steps and both photo/text calls is uniformly
    the Page token (`page_token`) — the brief left this token choice open
    ("if any detail conflicts with what you know, ask me instead of
    silently choosing"); using the one credential this module already has
    for every Page-scoped call is consistent and doesn't conflict with the
    documented shapes, so it wasn't treated as a case needing to ask.
  - **Error classification, extended from `meta.py` rather than
    duplicated**: `app/publishers/meta.py`'s `_raise_for_graph_error` was
    renamed to `raise_for_graph_error` (no longer module-private — it's a
    cross-module contract now) and gained a `token_invalid_error_class`
    parameter (default `PermanentError`, unchanged behavior for every
    existing call site in `meta.py`, including `refresh_stored_credentials`
    — its own tests were untouched by this). `facebook.py` calls it with
    `token_invalid_error_class=TokenExpiredError` on every publish-time
    Graph call, so a `code=190` (`OAuthException`) error raised while
    actually posting content becomes `TokenExpiredError` instead of a plain
    `PermanentError` — everything else (429/5xx/rate-limit-code ->
    `TransientError`, other 4xx -> `PermanentError`) is unchanged from
    `meta.py`'s existing table.
  - **Reactive token refresh wired for free**: `app/tasks.py`'s
    `_TOKEN_REFRESH_MODULES_BY_PLATFORM["facebook"]` already pointed at
    `meta.py` (Phase 23, for the proactive Beat refresh) — since
    `_handle_token_expired` (Phase 21) is already generic over any platform
    whose module exposes `refresh_stored_credentials`, wiring
    `facebook.py` to raise `TokenExpiredError` was the *only* change needed
    to get the same refresh-persist-retry-once behavior Twitter has; no
    edits to `app/tasks.py`'s refresh logic itself were required. Confirmed
    end-to-end (refresh succeeds and retries; refresh token permanently
    invalid deactivates the account and alerts; a transient refresh error
    or no `account_id` falls back to normal backoff) by
    `tests/test_tasks_facebook_token_refresh.py`, modeled directly on
    `tests/test_tasks_twitter_token_refresh.py`.
- **Dashboard NEW JOB form**: `facebook` added to `_SUPPORTED_PLATFORMS`
  and to `_ACCOUNT_REQUIRED_PLATFORMS` (`dashboard/api.py`, alongside
  `tiktok` — both have no single-account fallback, so the route now gives a
  friendly 400 instead of letting the job get created and fail later in the
  worker). Facebook's fields mirror Twitter's shape (`text` +
  optional media) but with different requirements: at least one of `text`/a
  single media file must be present (neither is independently required),
  and at most one media file is accepted (checked before upload, so a
  bad multi-file selection fails fast instead of writing files nobody will
  use) — `app/publishers/facebook.py` still owns the actual photo-vs-video
  detection and every further validation. `dashboard/static/index.html`
  gained a matching `#job-facebook-fields` block (text input + single-file
  input, shown only when `platform=facebook`) and reuses the same
  "select an account" placeholder Twitter/TikTok already had, generalized
  into `ACCOUNT_REQUIRED_PLATFORMS` on the frontend to match the backend.
- `scripts/enqueue_facebook_test.py` (new, modeled on
  `enqueue_twitter_test.py`) — `--mode text|photo|video`, `--file` for
  photo/video, `--account NAME` **required** (no single-account fallback,
  unlike Twitter's/YouTube's equivalent scripts). Dispatches immediately
  via the normal queue.

  **When a real Meta App and credentials arrive** (same checklist as Phase
  23, now actually exercisable end-to-end):
  1. Complete Phase 23's checklist (`scripts/authorize_meta.py`) to get a
     `facebook` Account row with a real `page_id`/`page_token`.
  2. Set `META_APP_ID` in `.env` if not already set (needed for the
     resumable video upload session, step 1 above).
  3. Test with `python -m scripts.enqueue_facebook_test --mode text
     --account "Main Page"`, then photo/video modes once that works.
  4. Watch a reactive refresh happen naturally the first time the stored
     Page token is rejected mid-publish (or force one by trying an
     intentionally stale token), and confirm rotation persists correctly
     onto the `Account` row (`scripts/show_accounts.py`).

### Phase 25 (current)
- **Instagram publisher** (`app/publishers/instagram.py`) — the
  container/publish flow built on Phase 23's Meta OAuth foundation, built
  "ready waiting for credentials" like Phases 21/23/24 (no real Meta App
  yet, everything exercised only against fully mocked HTTP —
  `tests/test_publisher_instagram.py`,
  `tests/test_tasks_instagram_token_refresh.py`,
  `tests/test_dashboard_media_staging.py`). Wired into
  `app/tasks.py::_PUBLISHERS_BY_PLATFORM["instagram"]`.
  - **No local-file upload path — the one publisher in this project that
    doesn't have one.** Meta downloads the media from a public URL at
    publish time ("media must be hosted on a publicly accessible server"),
    so this publisher only ever reads `payload["media_public_url"]` — the
    same field Phase 22's R2 staging (`app/storage.py::upload_file`)
    attaches for youtube/tiktok/facebook's single-media-file case. A
    missing `media_public_url` is a `PermanentError` naming exactly why
    (Instagram has no text-only post type, and R2 must be configured for a
    public URL to exist at all) — there is no fallback to a local
    `media_paths` file the way every other publisher has.
  - **Credentials**: `account_credentials` needs `ig_user_id` + `page_token`
    (Phase 23's `instagram` Account shape; `user_token` is only used by
    `meta.py`'s OAuth refresh flow, not by this module directly). No
    env-var single-account fallback, same as `facebook.py` — every
    instagram job needs an `Account` row from `scripts/authorize_meta.py`.
  - **Three-call container flow, endpoint shapes given directly in the
    phase brief, not guessed**:
    1. `_create_container`: `POST /<IG_USER_ID>/media` —
       `image_url=<url>&caption=<text>` for an image, or
       `media_type=REELS&video_url=<url>&caption=<text>` for a video
       (since July 2023 all single feed videos publish as Reels, so the
       video branch always sends `media_type=REELS`) -> `{"id":
       "<container_id>"}`. Image vs. video is auto-detected from
       `media_public_url`'s guessed MIME type
       (`mimetypes.guess_type`) — no separate "kind" flag, same spirit as
       `facebook.py`'s `_media_kind`.
    2. `_wait_for_container`: `GET /<CONTAINER_ID>?fields=status_code`,
       polled every `_POLL_INTERVAL_SECONDS` (5s) up to
       `_POLL_TIMEOUT_SECONDS` (300s) total. `IN_PROGRESS` -> keep polling;
       `FINISHED` -> proceed to step 3; `ERROR`/`EXPIRED` ->
       `PermanentError` (the only recovery is a fresh container, which is
       exactly what happens if the job is retried, since no `container_id`
       is persisted between attempts — publish_job would build a brand new
       one). A poll **timeout** is a `TransientError`, not permanent: the
       container may just need more time, and Celery's retry can pick the
       job up again later.
    3. `_publish_container`: `POST /<IG_USER_ID>/media_publish`,
       `creation_id=<container_id>` -> `{"id": "<ig_media_id>"}` — the post
       is only actually live after this step.
  - **Error classification, extended from `meta.py` exactly like
    `facebook.py` does**: every publish-time Graph call here passes
    `token_invalid_error_class=TokenExpiredError` to
    `meta.raise_for_graph_error`, so a `code=190` (`OAuthException`) error
    becomes `TokenExpiredError` instead of `meta.py`'s default
    `PermanentError`. `app/tasks.py`'s
    `_TOKEN_REFRESH_MODULES_BY_PLATFORM["instagram"]` already pointed at
    `meta.py` since Phase 23 (originally only reachable by the proactive
    Beat refresh, since there was no `publish()` to ever raise
    `TokenExpiredError`) — Phase 25 is what makes the **reactive**
    refresh-and-retry-once path (`_handle_token_expired`, Phase 21)
    actually reachable for instagram, with no changes needed in
    `app/tasks.py` beyond registering the publisher itself. Confirmed
    end-to-end by `tests/test_tasks_instagram_token_refresh.py`, modeled
    directly on `tests/test_tasks_facebook_token_refresh.py`.
  - **Not implemented**: the informational rate-limit endpoint (`GET
    /<IG_USER_ID>/content_publishing_limit`, ~100 API-published posts/24h
    per account) — nothing here reads it proactively; exceeding it just
    surfaces as an ordinary classified Graph error from `_create_container`.
  - **Not implemented, follow-up candidate**: `app/media_probe.py`'s
    ffprobe-based pre-flight validation of Reels specs (MP4/MOV, aspect
    ratio 0.01:1–10:1, 9:16 recommended), the same way
    `app/publishers/youtube.py` validates Shorts (Phase 15). `media_probe.probe()`
    operates on a local file path via an `ffprobe` subprocess, but this
    publisher only ever has a public R2 URL, never a local path — adding
    this would mean downloading the file back from R2 before probing it,
    an extra network round-trip and more moving parts than fits this
    phase. Meta's own container processing already rejects a malformed
    video via `status_code=ERROR` (caught above), so skipping this is a
    fail-fast/UX gap, not a correctness one. Revisit if bad-media jobs
    start burning API-published-post quota before failing.
- **Dashboard NEW JOB form**: `instagram` added to `_SUPPORTED_PLATFORMS`
  and `_ACCOUNT_REQUIRED_PLATFORMS` (`dashboard/api.py`, alongside
  `tiktok`/`facebook` — no single-account fallback). Unlike every other
  platform, a failed/unconfigured R2 staging attempt is **not** swallowed
  for instagram: `create_job` returns a 400 naming the missing R2 env vars
  instead of creating a job that's guaranteed to dead-letter in the worker
  (every other platform still degrades gracefully to local-path-only, per
  Phase 22 — instagram has no local-path fallback to degrade to). Exactly
  one media file is required (no text-only posts); an optional `text`
  caption is allowed alongside it. `dashboard/static/index.html` gained a
  matching `#job-instagram-fields` block (caption input + required
  single-file input, plus an inline note about the R2 requirement) and
  extends the frontend's own `ACCOUNT_REQUIRED_PLATFORMS` set to match.
- `scripts/enqueue_instagram_test.py` (new, modeled on
  `enqueue_facebook_test.py`) — `--mode image|video` (informational only;
  the publisher auto-detects the real type), `--file`, `--account`
  (**required**, no single-account fallback), optional `--caption`. Unlike
  every other `enqueue_*_test.py` script, this one calls
  `app/storage.py::upload_file` itself before creating the job, since
  `instagram.py` needs `media_public_url` in the payload, not a local
  `media_paths` entry — it fails loudly if R2 isn't configured, the same
  way the publisher itself would.

  **When a real Meta App and credentials arrive** (same checklist as Phase
  23/24, now covering Instagram too):
  1. Complete Phase 23's checklist (`scripts/authorize_meta.py`), making
     sure the target IG account is Business/Creator and linked to the
     Facebook Page *before* running it — this is what creates the
     `instagram` Account row alongside the `facebook` one.
  2. Confirm R2 is fully configured (`R2_ENDPOINT_URL`/`R2_ACCESS_KEY_ID`/
     `R2_SECRET_ACCESS_KEY`/`R2_BUCKET_NAME`/`R2_PUBLIC_BASE_URL`, Phase
     22) — instagram jobs cannot work without it.
  3. Test with `python -m scripts.enqueue_instagram_test --mode image
     --file photo.jpg --account "Main Page"`, then `--mode video` once
     that works.
  4. Watch a reactive refresh happen naturally the first time the stored
     Page token is rejected mid-publish, and confirm rotation persists
     correctly onto the `Account` row (`scripts/show_accounts.py`) — same
     mechanism as Facebook's (Phase 24), since both platforms share
     `meta.py`'s refresh logic.

### Phase 26 (in progress)
- **Client model + scheduler UI foundation** — start of the "Postline"
  scheduling tool from `design_handoff_scheduler/` (a high-fidelity design
  handoff: `README.md` + `Scheduler.dc.html`, a reference-only prototype),
  which replaces `dashboard/static/index.html`'s monitoring-only UI with a
  full calendar/queue/composer/approvals/analytics/media-library/accounts/
  clients tool. Built screen-by-screen on top of the existing
  `dashboard/api.py` + `app/models.py` foundation, starting with the two
  screens that needed no new backend concepts (Queue, Calendar).
  - **`app/models.py::Client`** (new table) — a workspace this engine
    publishes on behalf of (`name`, `kind`: `"individual"` | `"client"`),
    backing the design's client-switcher. Deliberately minimal — dashboard-
    only grouping metadata, never read by `app/tasks.py` or any publisher.
    `Account.client_id` and `Job.client_id` (both new, nullable FKs) scope
    accounts and jobs to a client. **`Job.client_id` is independent of
    `Account.client_id`/`account_id`** rather than derived through the
    account — a job can exist for a client before any `Account` is
    connected (single-account/env-var-fallback platforms like
    youtube/twitter), and the scheduler UI scopes every screen by the
    active client directly. Both are additive/nullable, same pattern as
    `account_id` (Phase 6): existing rows and jobs/accounts created without
    a client keep working unchanged.
  - **Manual schema step**, same pattern as every prior additive-column
    phase (`account_id`, `external_id`, `last_stall_alert_at`): `init_db()`'s
    `create_all` creates the new `clients` table automatically on an
    existing Neon database, but does **not** add the new `client_id` columns
    to the existing `accounts`/`jobs` tables. Run once, by hand, against
    Neon:
    ```sql
    ALTER TABLE accounts ADD COLUMN client_id INTEGER REFERENCES clients(id);
    ALTER TABLE jobs ADD COLUMN client_id INTEGER REFERENCES clients(id);
    ```
  - **`dashboard/api.py`**: `GET /api/clients`, `POST /api/clients`
    (name + kind, no dedup/update/delete yet — narrow enough that adding
    those later is additive). `GET /api/jobs` and `GET /api/accounts` both
    gained an optional `client_id` filter and now return `client_id` +
    `client_name` (resolved via a single extra query per request, not a
    per-row query). `JobOut` also gained `caption` — a best-effort display
    string extracted from `payload` by `_extract_caption` (checks `text`,
    then `thread[0].text`, then `title`, then `caption`, in that order;
    `None` if nothing matches) — the design's Queue/Calendar/Approvals
    screens all show one line of post content per job, and payload shape
    varies by platform (see each publisher's phase above) so this can't be
    a single column. `POST /api/jobs` gained an optional `client_id` form
    field, validated to exist (404 if not) and stored directly on the
    created `Job` — independent of whichever `account_id` was also passed,
    per the design decision above.
  - **Frontend**: `dashboard/static/index.html` rewritten from scratch
    against the design's tokens (`design_handoff_scheduler/README.md`'s
    Design Tokens section — Barlow/Barlow Condensed fonts, `#f2f2f3`
    background, steel-blue `#5980a6` accent, hairline `rgba(29,31,32,0.16)`
    borders, square corners, no drop shadows), replacing the old
    neobrutalist look entirely (violet header, black hard-shadow borders) —
    same single-file-vanilla-JS-no-build-step convention as before, just a
    different visual system. Persistent sidebar (all 7 nav items from the
    design) + topbar (client switcher, screen title, "+ New post") render
    on every screen. **Calendar** (month grid + agenda view, toggle is
    local UI state) and **Queue** (status filter chips + table with Retry)
    are fully wired to `/api/jobs`, `/api/clients`, and
    `POST /api/jobs/{id}/retry`. Every other nav item (Composer, Approvals,
    Analytics, Media library, Connected accounts beyond a basic list,
    Clients grid beyond the switcher, Onboarding) renders a "not built yet"
    placeholder in the content area — the nav item is clickable and
    highighted like the design, it just doesn't have a real screen behind
    it yet. Client switching persists via `activeClientId` in local state
    and re-filters Calendar/Queue's `/api/jobs` calls by `client_id`; a
    brand-new install has zero `Client` rows, so the switcher's own
    dropdown includes an inline "+ Add client workspace" action
    (`POST /api/clients`) since there's no separate Clients screen to do it
    from yet.
  - **Explicitly deferred to later phases** (each flagged in the design
    handoff as needing a decision or new backend work before building):
    Approvals (no "pending approval" concept exists in `JobStatus` today —
    needs a new state or flag, plus somewhere to store a rejection reason),
    Composer's "Save draft" (no `DRAFT` status), Media library (no `Media`
    model — R2 storage exists but nothing tracks dimensions/filename/
    usage independently of a job payload), Connected accounts' `@handle`
    display (`Account` has no `handle` field, only `name`), real in-browser
    OAuth onboarding (today's authorization flow is CLI scripts —
    `scripts/authorize_meta.py` etc. — that pop a browser and bind a
    one-shot local HTTP server, which doesn't translate directly into a
    web dashboard flow), and Pinterest (in the design's platform set, no
    publisher exists — `app/publishers/`).

### Phase 28 (current)
- **Multi-tenant user accounts: self-registration + admin approval** — up
  to now the whole dashboard was gated by one shared credential pair
  (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`, Phase 11). This phase adds
  real per-person logins for client teams (~100-200 users), each scoped to
  exactly one `Client` (Phase 26) — a client user can never read or write
  another client's jobs/accounts, even via a direct API call with a guessed
  id. The Basic-Auth env-var credential is unchanged and remains a fully
  separate "ops" admin mechanism (curl/scripts/`/docs`) — see below.
- **`app/models.py::User`** (new table) — `email` (unique), `hashed_password`
  (bcrypt, `app/auth.py::hash_password`, never plaintext), `role`
  (`"admin"` | `"client_user"`, free string like `Job.platform`/
  `Client.kind`), `client_id` (nullable FK to `clients` — null for
  `role="admin"` and for a still-pending registration),
  `requested_client_name` (free text the registrant typed, see below),
  `is_approved` (bool, default `False`), `created_at`.
  - **No manual Neon migration step this time** — unlike Phase 26's
    `client_id` columns (added to the *existing* `jobs`/`accounts` tables,
    which `create_all` can't retroactively `ALTER`), `users` is a brand
    new table. `init_db()`'s `create_all` creates it automatically on an
    existing Neon database, the same way it already creates `clients`
    without a manual step. Nothing needs to be run by hand for this phase.
- **`app/auth.py`** (new) — pure password/token helpers, kept in `app/`
  rather than `dashboard/` because it's `User`-domain logic (same reasoning
  as the model itself living in `app/models.py`), even though only
  `dashboard/api.py` calls it today.
  - `hash_password`/`verify_password` — bcrypt, via the `bcrypt` package
    directly (not `passlib`, to avoid its bcrypt-backend version-detection
    issues with modern `bcrypt` releases).
  - `create_session_token`/`verify_session_token` — a signed, timestamped
    token (`itsdangerous.URLSafeTimedSerializer`) encoding a `user_id`,
    valid for `SESSION_MAX_AGE_SECONDS` (7 days). Signed with
    `SESSION_SECRET_KEY` (env var, optional at the `Settings` layer like
    the `R2_*` vars); if unset, falls back to a random per-process key and
    warns loudly at import time (same style as every other "works locally,
    loud in prod" warning in this project) — every session is invalidated
    on the next restart until this is set for real.
- **Session cookie, not JWT** (`dashboard/api.py`) — `de_session`, httponly
  (JS can never read it), `SameSite=Lax`, `Secure` by default
  (`SESSION_COOKIE_SECURE`, default `true`; set to `false` for local dev
  over plain `http://localhost`, which logs the same style of loud warning).
  **Security tradeoff, deliberately made**: no separate CSRF token —
  `SameSite=Lax` already blocks the cookie from being sent on a cross-site
  request, which covers this phase's actual attack surface (a same-origin
  SPA calling its own API); a dedicated CSRF token was judged unnecessary
  complexity on top of that, per the phase brief's "keep it simple"
  instruction. Revisit if a cross-site embed/widget ever needs this API.
- **Two parallel, independent admin mechanisms — by design**:
  1. **Basic-Auth "ops" admin** (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`,
     unchanged from Phase 11) — still grants full, unscoped access on any
     request carrying valid Basic credentials. No `User` row involved.
     This is what curl/scripts/CI and the auto-generated `/docs` use.
  2. **Admin `User` row** (`role="admin"`, `client_id=None`,
     `is_approved=True`) — logs in through the same `/api/auth/login` form
     as a client user, gets a session cookie, and is treated identically to
     the Basic-Auth path by every scoped endpoint (`role == "admin"` either
     way). **The only way to create one is `scripts/create_admin_user.py`**
     — self-registration always creates `role="client_user"`, and the
     admin-approval flow only ever assigns a `Client` to an existing
     pending request, never promotes anyone to `role="admin"`.
  This split exists because the phase brief requires admin's *existing*
  mechanism to stay unchanged, while also wanting a real browser Login
  screen instead of the OS-native Basic-Auth popup — which the static SPA
  shell no longer triggers at all (see below), so a human admin normally
  uses option 2 day-to-day and option 1 stays available for tooling.
- **`dashboard/api.py`'s auth middleware rewritten**
  (`enforce_basic_auth` -> `enforce_auth`): resolves an `AuthContext`
  (`role`, `client_id`, `user_id`, `email`) once per request — Basic
  header first, then the session cookie, else `"anonymous"` — and stashes
  it on `request.state.auth` regardless of whether the path is gated, so
  `GET /api/auth/me` can report real identity on an otherwise-public route.
  - **The static SPA shell is now public** (`index.html`/JS/CSS) — the
    biggest structural change here. Before this phase, gating *everything*
    meant an anonymous visitor got the browser's native Basic-Auth popup
    before ever seeing any of this app's own UI, which would have made a
    custom Login screen unreachable. The shell contains no secrets, so
    exempting it is the same tradeoff any SPA makes. `/health` and
    `POST /webhooks/tiktok` were already exempt (Phase 9/10b) and still are.
  - **`/api/auth/register`, `/api/auth/login`, `/api/auth/logout`,
    `/api/auth/me` are also public** (`_PUBLIC_API_PATHS`) — the first
    three are inherently pre-session, and `/me` is how the SPA silently
    checks "am I logged in?" on load (`200 {"authenticated": false}`
    instead of a `401`, so anonymous visitors just see the Login screen).
  - **`WWW-Authenticate: Basic` is now scoped to `/docs`/`/redoc`/
    `/openapi.json` only** — deliberately dropped from every other gated
    `/api/*` 401. That header is what triggers a browser's native
    credential popup on *any* 401 response (not just an explicit login
    attempt), which would otherwise hijack a client_user's own Login screen
    the moment a `fetch()` call happened to 401 (e.g. an expired session
    mid-use). `/docs` is still opened directly in a browser tab by admins,
    so the old native-popup UX is kept there on purpose.
- **`POST /api/auth/register`** — public, always creates
  `role="client_user"`, `is_approved=False`. Takes `client_name` (free
  text), not `client_id` — the requester has no way to know internal ids.
  **Deliberately no public client-search/autocomplete endpoint** was added
  for this: exposing the full agency client roster to anyone hitting a
  public, unauthenticated route was judged a worse tradeoff than a plain
  text field the admin reconciles at approval time (`requested_client_name`
  is only ever a *hint* — the admin picks the real `Client` from a dropdown
  they already have full access to). **No user enumeration**: a duplicate
  email returns the exact same response (`{"status": "submitted", ...}`,
  same status code) as a genuine new registration — this is a deliberate
  silent no-op, not a distinguishable error, checked by
  `tests/test_dashboard_auth.py::TestRegister::test_duplicate_email_returns_identical_response_and_no_new_row`.
- **`POST /api/auth/login`** — public, checked only against the `User`
  table (never the Basic-Auth env vars — see the two-mechanisms note
  above). **Security tradeoff, explicitly flagged**: an unknown email and
  a wrong password return the exact same generic 401
  (`"Invalid email or password."`), preventing enumeration via login
  attempts. A **pending** (`is_approved=False`) account gets a distinctly
  worded 403 (`"...pending admin approval."`) instead, per the phase
  brief's explicit ask for a clear message — this narrowly reveals that
  the email *is* registered-but-pending, a deliberate, smaller exception
  to the no-enumeration rule above rather than applying it everywhere
  uniformly. On success, sets the session cookie and returns
  `{authenticated, role, client_id, client_name, email}`.
- **Per-client scoping, enforced at the query level, not just hidden in
  the UI** (`dashboard/api.py`) — every one of these treats an explicit,
  *different* `client_id` from a `client_user` as a `403`, and a missing
  one as "silently forced to their own":
  - `GET /api/jobs`, `GET /api/accounts`, `GET /api/stats` (`stats` gained
    an optional `client_id` filter it didn't have before this phase).
  - `POST /api/jobs/{id}/retry` — 403 if the job's `client_id` isn't
    theirs.
  - `POST /api/jobs` — `client_id` is forced to the caller's own (an
    explicit different value is rejected, not silently overridden, so a
    buggy/malicious client isn't quietly redirected). **Also validates
    `account_id` cross-tenant**: if the job names an `Account` belonging to
    a different client, that's a `403` too — without this check a
    `client_user` could *post content through* another client's connected
    social account (not just read its data), a materially worse leak than
    a read-only cross-tenant view.
  - `GET /api/clients` / `POST /api/clients` — made **admin-only**
    (`403` for `client_user`), a scoping decision not explicitly spelled
    out in the phase brief but a natural extension of "must never see
    another client's data" to the client roster itself.
  - Every scoped route function takes `request: Request | None = None`
    (special-cased by FastAPI to always inject the real request over HTTP,
    regardless of the default) rather than a `Depends`-based dependency —
    deliberately, so every one of them stays callable directly with no
    request at all, the same way every existing test in this suite (e.g.
    `tests/test_dashboard_media_staging.py::_run_create_job`) already
    calls route functions, bypassing the HTTP layer/middleware entirely. A
    direct call with `request=None` resolves to `ADMIN_AUTH` (trusted/
    internal caller), preserving every pre-Phase-28 test unchanged.
  - **Accepted enumeration side-channel**: a cross-tenant `403` (vs. a
    `404` for a genuinely nonexistent id) confirms *that* a given job/
    account/user id exists, just not its contents — judged an acceptable
    tradeoff since the phase brief explicitly asked for `403` on
    cross-tenant access, and the ids involved (autoincrement integers)
    aren't secret/guess-resistant identifiers to begin with.
- **Admin endpoints** (`dashboard/api.py`, all `403` for `client_user` via
  `_require_admin`): `GET /api/admin/users/pending`,
  `GET /api/admin/users` (approved, read-only — **editing role/client or
  deactivating an approved user isn't built this phase**, flagged as a
  follow-up), `POST /api/admin/users/{id}/approve` (body `{client_id}` —
  the admin picks/confirms the real `Client`, using `requested_client_name`
  only as a pre-selected guess if an exact case-insensitive name match
  exists), `POST /api/admin/users/{id}/reject` (deletes the row — `400` if
  the user is already approved, so the wrong button can't delete a live
  account; deactivating an approved user isn't built this phase either).
- **`scripts/create_admin_user.py`** (new) — the only way to bootstrap an
  admin `User` row. Prompts for the password interactively via `getpass`
  (twice, confirmed) rather than a CLI arg, so it never lands in shell
  history. Re-running with the same `--email` resets that admin's password
  and forces `role="admin"`/`client_id=None`/`is_approved=True` in place,
  same upsert-by-identity spirit as `scripts/add_account.py`.
  ```
  python -m scripts.create_admin_user --email admin@example.com
  ```
- **Dashboard frontend** (`dashboard/static/index.html`) — new Login and
  Register screens (`#auth-root`, shown instead of `#app-root` whenever
  `GET /api/auth/me` reports `authenticated: false`), matching the Phase
  26 design tokens. A `client_user` session hides the client-switcher
  (replaced with a static label reading their own `client_name` from
  `/api/auth/me` — no dropdown, no "+ Add client workspace", per the phase
  brief) and the Admin nav item entirely (not just disabled — the
  underlying endpoints `403` too, so this is UI convenience, not the real
  boundary). A new Admin screen (admin-only, `403`+placeholder for anyone
  else) lists pending registrations with a per-row client-assignment
  `<select>` + Approve/Reject, and a read-only approved-users table.
  - **Found and fixed while browser-testing this phase** (pre-existing,
    not part of Phase 28's own diff, in the uncommitted Composer/Calendar
    work): the client-switcher dropdown opened and instantly closed on the
    same click. Cause: the "close on outside click" `document`-level
    listener checked `wrap.contains(e.target)`, but the button's own click
    handler synchronously re-rendered (replacing the button DOM node)
    *before* that listener ran, leaving `e.target` a detached node that
    `.contains()` always reports `false` for. Fixed with
    `e.stopPropagation()` on the button's own click handler.
  - `POST /api/auth/register`/`login` read their form fields *before*
    calling `renderAuthScreen()` on submit — calling it first (to show a
    "submitting…" disabled state) would wipe the uncontrolled `<input>`
    values it's about to read, since re-rendering regenerates the form's
    `innerHTML` from scratch. Caught by browser-testing the actual submit
    flow (Playwright), not by the unit-style pytest suite, which never
    drives the real DOM.
  - `state.screen` resets to `"calendar"` on login/logout — without this,
    switching identities in the same tab (logout then log back in as a
    different role) could land the next session on whatever screen the
    previous one was viewing (harmless — a `client_user` landing on
    `"admin"` just sees the "isn't built yet" placeholder, not real admin
    content, since the screen router itself checks the role — but
    confusing UX).
- **Tests**: `tests/test_auth.py` (password hash/verify, session token
  round-trip/tamper/garbage — pure, no DB). `tests/test_dashboard_auth.py`
  uses FastAPI's `TestClient` (real HTTP + middleware + cookies), unlike
  the rest of this suite which calls route functions directly — this phase
  is specifically about request-level behavior (the middleware, cross-
  tenant `403`s), which a direct function call bypasses entirely. Covers:
  public-path/`WWW-Authenticate` behavior, registration (happy path,
  duplicate-email no-op, validation), login (happy path, wrong password,
  unknown email, pending account), full cross-tenant scoping (jobs,
  accounts, stats, retry, job creation incl. the cross-tenant `account_id`
  check, `/api/clients`, admin routes all `403` for a `client_user`), and
  the admin approve/reject flow. `tests/conftest.py` gained
  `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` (so the suite exercises real
  auth instead of the dev-mode bypass), `SESSION_SECRET_KEY`, and
  `SESSION_COOKIE_SECURE=false` (FastAPI's `TestClient` talks plain HTTP to
  `http://testserver`, so a `Secure`-flagged cookie would never round-trip
  through its cookie jar — same reason a real browser wouldn't send one
  back over `http://localhost` either).

### Phase 29a (current)
- **In-browser YouTube OAuth for client self-service** — the start of
  replacing CLI-only onboarding (`scripts/authorize_youtube.py`) with a
  browser flow a `client_user` can complete themselves, without shell
  access. Scoped narrowly to YouTube and to `role="client_user"` sessions
  only (not admin — an admin isn't scoped to any one `Client`, and this
  flow always creates the resulting `Account` under the caller's own
  `client_id`). Other platforms (twitter/tiktok/facebook/instagram) still
  require the CLI `scripts/authorize_*.py` scripts — a natural follow-up,
  not built this phase.
- **`app/publishers/youtube.py`** gained two new pure functions, the
  web-flow (browser redirect) counterpart to
  `scripts/authorize_youtube.py`'s `InstalledAppFlow`, built on
  `google_auth_oauthlib.flow.Flow` (already a dependency):
  - `build_authorization_url(redirect_uri, state)` — builds the Google
    consent-screen URL. `state` is opaque to this module (round-tripped by
    Google verbatim) — the caller is responsible for making it
    tamper-evident.
  - `exchange_code_for_credentials(code, redirect_uri)` — exchanges an
    authorization code for credentials JSON (same shape as
    `Credentials.to_json()`, the module's existing "Credentials JSON
    shape").
  Both raise `PermanentError` if `client_secret_web.json` is missing at
  `WEB_CLIENT_SECRET_PATH` — same fail-clearly posture as every other
  credential-dependent path in this module. **`WEB_CLIENT_SECRET_PATH` is a
  distinct file/constant from `CLIENT_SECRET_PATH`** (`client_secret.json`,
  used by `scripts/authorize_youtube.py`'s `InstalledAppFlow` and the
  single-account fallback) — fixed shortly after this phase shipped, once it
  became clear the two needed different registered redirect URIs: **the
  registered OAuth Client ID for the web flow must be a "Web application"
  type, not "Desktop app"** — Google validates `redirect_uri` against the
  client type, and a Desktop-app client rejects a web redirect URI outright.
  Reusing `CLIENT_SECRET_PATH` for both would have meant one OAuth Client ID
  had to serve two incompatible redirect-URI types. Every other function in
  this module (the CLI single-account fallback, playlist/Shorts logic, etc.)
  is unaffected and keeps reading `CLIENT_SECRET_PATH` exactly as before.
- **`app/auth.py`** gained `create_oauth_state_token`/
  `verify_oauth_state_token` — signs the OAuth `state` param through the
  external Google redirect (which carries no session cookie back), reusing
  `_SESSION_SECRET_KEY` but a distinct salt
  (`"distribution-engine-oauth-state"`) from the session-cookie signer, so
  a session token and an OAuth-state token can never be replayed as each
  other. Short-lived (10 minutes, `OAUTH_STATE_MAX_AGE_SECONDS`) — it only
  needs to survive one round trip through Google's consent screen.
- **Two new routes in `dashboard/api.py`**:
  - `GET /api/oauth/youtube/start` (gated, `client_user`-only — `403` for
    admin or an unscoped caller) — builds `redirect_uri` from
    `request.url_for("youtube_oauth_callback")` (so it's always exactly
    this server's own callback URL, whatever host it's running on), signs
    `{"client_id", "user_id"}` into `state`, and redirects the browser to
    Google's consent screen.
  - `GET /api/oauth/youtube/callback` (public — added to
    `_PUBLIC_API_PATHS`, since Google's redirect carries no session cookie
    at all; this route's only trust anchor is the signed `state` param, not
    `enforce_auth`) — verifies `state`, exchanges `code` for credentials,
    and upserts an `Account` row (`platform="youtube"`, named
    `"<Client name> (self-service)"`) via the same
    `scripts/add_account.py::upsert_account` helper every other
    authorize script uses, with `client_id` set from the verified state —
    so reconnecting the same channel rotates its token in place instead of
    creating a duplicate `Account`. **Always redirects back into the SPA**
    (`/?screen=accounts&youtube_connect=success` or
    `...=error&reason=<code>`), never a raw JSON error response, since this
    is a full-page browser navigation, not a `fetch()` call.
- **Frontend** (`dashboard/static/index.html`): a "Connect YouTube" button
  on the Connected Accounts screen, shown only for a `client_user` session
  (`<a href="/api/oauth/youtube/start">`, a plain top-level navigation so
  the session cookie rides along and the redirect chain to Google and back
  works without any JS-side fetch/CORS handling). `boot()` gained
  `consumeOauthRedirectParams()`, which reads `?screen=...&youtube_connect=
  success|error&reason=...` left by the callback route's redirect, sets
  `state.screen` and a one-time notice banner, and strips the query string
  via `history.replaceState` so a page refresh doesn't re-show it.
- **Tests**: `tests/test_auth.py::TestOAuthStateTokens` (roundtrip, tamper,
  garbage, and — the one specific to this being a second signer — a valid
  *session* token is rejected as OAuth state). `tests/test_publisher_youtube.py::TestWebOAuthFlow`
  (`Flow.from_client_secrets_file` mocked with an in-memory fake, no real
  Google HTTP — missing-`client_secret_web.json` at `WEB_CLIENT_SECRET_PATH`
  on both functions, the authorization URL/params built correctly, code
  exchange returning parsed credentials JSON, a `Flow.fetch_token` failure
  wrapped as `PermanentError`). `tests/test_dashboard_youtube_oauth.py` (FastAPI
  `TestClient`, same reasoning as `tests/test_dashboard_auth.py` — this is
  request-level behavior): `/start` 401 anonymous / 403 admin / redirects a
  `client_user` to the (mocked) Google URL with a verifiable signed state;
  `/callback` handles Google's `error` param, missing code/state, a
  tampered state, a state naming a nonexistent client, a full happy path
  asserting the created `Account`'s `client_id`/`credentials`, reconnecting
  rotating the same `Account` instead of duplicating it, and a mocked
  exchange failure leaving no `Account` behind.

  **Local dev setup — register this exact redirect URI in Google Cloud
  Console** (OAuth Client ID type: **Web application** — a genuinely
  separate Client ID from the "Desktop app" one `scripts/authorize_youtube.py`
  uses, since Google rejects a web redirect URI on a Desktop-app client):
  ```
  http://localhost:8000/api/oauth/youtube/callback
  ```
  (Adjust the host/port if the dashboard runs elsewhere locally —
  `/api/oauth/youtube/start` derives `redirect_uri` from the incoming
  request, so it always matches whatever's actually registered as long as
  that's what's typed into the browser.) In production this needs the
  real deployed origin's equivalent (`https://<fly-app>.fly.dev/api/oauth/youtube/callback`
  or a custom domain) added as an additional authorized redirect URI once
  deployed. Download this Web-application Client ID's JSON as
  **`client_secret_web.json`** at the project root (`WEB_CLIENT_SECRET_PATH`)
  — a separate file from the Desktop-app `client_secret.json`
  (`CLIENT_SECRET_PATH`) the CLI script uses; both are gitignored/
  dockerignored.

### Phase 29b (current)
- **In-browser X/Twitter OAuth for client self-service** — same pattern as
  Phase 29a (YouTube), extended to Twitter/X. Scoped narrowly the same way:
  `role="client_user"` sessions only, always creates the resulting `Account`
  under the caller's own `client_id`. Other platforms (tiktok/facebook/
  instagram) still require their CLI `scripts/authorize_*.py` scripts.
  Unlike YouTube, X had no CLI authorize script to begin with (Phase 21's
  note: "Twitter has no interactive authorize script yet... the alert
  points at obtaining a fresh authorization code via X's OAuth 2.0 PKCE flow
  by hand") — this phase is the first place X's Authorization Code + PKCE
  flow is actually implemented in code, not just documented as a manual
  procedure.
- **`app/publishers/twitter.py`** gained two new pure functions, mirroring
  `youtube.py`'s `build_authorization_url`/`exchange_code_for_credentials`
  shape exactly, and reusing Phase 21's existing token-endpoint plumbing
  (`_TOKEN_URL`, `_FORM_HEADERS`, `_raise_for_token_error`,
  `_compute_expiry`) rather than duplicating it:
  - `build_authorization_url(redirect_uri, state, code_challenge)` — builds
    X's `https://twitter.com/i/oauth2/authorize` consent-screen URL
    (`AUTHORIZE_URL`). Reads `TWITTER_CLIENT_ID` directly from the
    environment (app-level, no `Account` row exists yet at this point in
    the flow — same reasoning as `facebook.py` reading `META_APP_ID`
    directly). Raises `PermanentError` if unset.
  - `exchange_code_for_credentials(code, redirect_uri, code_verifier)` —
    `POST` to the same `_TOKEN_URL` as `refresh_stored_credentials`
    (`grant_type=authorization_code` instead of `refresh_token`),
    authenticated the same way (HTTP Basic, `client_id:client_secret` from
    `TWITTER_CLIENT_ID`/`TWITTER_CLIENT_SECRET`). Returns a credentials dict
    in the exact shape `_resolve_credentials`/`refresh_stored_credentials`
    already expect (`client_id`, `client_secret`, `access_token`,
    `refresh_token`, `expires_at`). **Every failure normalizes to
    `PermanentError`** (unlike `refresh_stored_credentials`, which
    distinguishes transient/permanent for the Beat task's benefit) — this is
    a one-shot interactive flow driven by a human at the consent screen, so
    the only thing a failure needs to do is tell `dashboard/api.py`'s
    callback route to show an error and let them click "Connect" again, the
    same contract `youtube.py`'s `exchange_code_for_credentials` already
    has.
  - **PKCE is required by X even for a confidential client** (unlike
    Google's web flow, which doesn't need it) — `build_authorization_url`
    takes a caller-supplied `code_challenge` and neither function generates
    the verifier/challenge pair itself (that's `dashboard/api.py`'s job, see
    below), since this module has no state/session concept to stash the
    verifier in between the two calls. **Standard RFC 7636 PKCE** —
    `code_challenge = BASE64URL(SHA256(verifier))`, method `S256` — this is
    the opposite of `scripts/authorize_tiktok.py`'s deliberate hex-digest
    deviation for TikTok; do not copy that trick onto X, which follows the
    RFC exactly.
- **`dashboard/api.py`**: `_generate_pkce_pair()` (new) — `secrets.token_urlsafe(64)`
  for the verifier, standard base64url-no-padding SHA-256 for the challenge.
  Two new routes mirroring `youtube_oauth_start`/`youtube_oauth_callback`
  (Phase 29a) exactly in shape:
  - `GET /api/oauth/twitter/start` (gated, `client_user`-only) — generates
    the PKCE pair, signs `{"client_id", "user_id", "code_verifier"}` into
    `state` (one field more than YouTube's, since X's callback never echoes
    the verifier back on its own — the signed state token is the only place
    it survives the round trip through X's consent screen), and redirects
    to X's authorize URL. **`redirect_uri` is rewritten from `localhost` to
    the loopback IP `127.0.0.1` before being sent to X**
    (`_use_loopback_ip_for_local_host`, added shortly after this phase
    shipped) — X's OAuth 2.0 authorize endpoint rejects a `localhost`
    redirect_uri outright with a generic "Something went wrong" error; for
    non-HTTPS local development it only accepts `127.0.0.1` (confirmed via
    X's own developer forum, not documented in its official OAuth docs).
    This is X-specific — YouTube's and TikTok's equivalent routes accept
    `localhost` directly and are unaffected. The rewrite only touches what's
    sent to X; the callback **route** itself is unchanged (FastAPI matches
    by path, not host) — once X redirects the browser back to the
    rewritten URI, `request.url_for` in `twitter_oauth_callback` naturally
    reproduces `127.0.0.1` too, since that's the real `Host` header on that
    follow-up request, so the `redirect_uri` passed to
    `exchange_code_for_credentials` still matches what X received in the
    original authorize request.
  - `GET /api/oauth/twitter/callback` (public — added to `_PUBLIC_API_PATHS`
    alongside the YouTube one, same reasoning: X's redirect carries no
    session cookie) — verifies `state`, pulls `code_verifier` back out of it
    (a validly-signed state token that happens to be missing this field —
    e.g. a stale pre-29b token — redirects with
    `reason=missing_pkce_verifier`, distinct from a tampered/expired
    signature), exchanges `code` + `code_verifier` for credentials, and
    upserts an `Account` row (`platform="twitter"`, named
    `"<Client name> (self-service)"`) via the same
    `scripts/add_account.py::upsert_account` helper every other authorize
    path uses — reconnecting rotates the same Account in place instead of
    duplicating it. Always redirects back into the SPA
    (`/?screen=accounts&twitter_connect=success` or `...=error&reason=<code>`),
    same as YouTube's callback.
- **Frontend** (`dashboard/static/index.html`): a second "Connect X/Twitter"
  button next to "Connect YouTube" on the Connected Accounts screen (both
  `client_user`-only, both plain `<a href="/api/oauth/.../start">`
  navigations so the session cookie rides along). `state.twitterConnectNotice`
  mirrors `state.youtubeConnectNotice`; `consumeOauthRedirectParams()`
  additionally reads `?twitter_connect=success|error&reason=...` and strips
  it the same way.
- **Tests**: `tests/test_publisher_twitter.py::TestWebOAuthFlow` (modeled on
  `tests/test_publisher_youtube.py::TestWebOAuthFlow` — missing
  `TWITTER_CLIENT_ID`/`_SECRET` on both functions, the authorization URL
  built with the right PKCE params, code exchange returning parsed
  credentials, a missing-`refresh_token` response and a token-endpoint
  rejection both wrapped as `PermanentError`).
  `tests/test_dashboard_twitter_oauth.py` (FastAPI `TestClient`, modeled on
  `tests/test_dashboard_youtube_oauth.py`): `/start` 401 anonymous / 403
  admin / redirects a `client_user` to the (mocked) X URL with a verifiable
  signed state, asserting the `code_challenge` sent to X actually derives
  from the `code_verifier` stashed in that state (RFC 7636 S256, checked
  independently in the test); `/callback` handles X's `error` param, missing
  code/state, a tampered state, a validly-signed state missing
  `code_verifier`, a state naming a nonexistent client, a full happy path
  asserting the created `Account`'s `client_id`/`credentials` (including
  that the right `code_verifier` reached the exchange call), reconnecting
  rotating the same `Account` instead of duplicating it, a simulated
  verifier/challenge mismatch at the token endpoint, and a generic exchange
  failure — both of the latter two leaving no `Account` behind.
- **Post-launch fix: `media.write` scope missing** — connecting via this
  flow worked and text-only tweets posted fine, but any post with
  `media_paths` failed with `HTTP 403 Forbidden` on X's
  `POST /2/media/upload` INIT call. Cause: `tweet.write` covers tweet
  creation but not the v2 media upload endpoint, which checks its own
  `media.write` scope. Fix: `_OAUTH_SCOPES` in `app/publishers/twitter.py`
  now requests `tweet.read tweet.write users.read offline.access
  media.write` (was missing `media.write`). **Accounts connected before
  this fix only hold the original four scopes and must reconnect** ("Connect
  X/Twitter" again on the Connected Accounts screen) to pick up
  `media.write` — there's no way to add a scope to an already-issued
  access/refresh token pair short of a fresh authorization.

  **Local dev setup — register this exact redirect URI in the X Developer
  Portal** (OAuth 2.0 app settings — same confidential client Phase 21 set
  up for `TWITTER_CLIENT_ID`/`TWITTER_CLIENT_SECRET`, "Type of App" must
  support the Authorization Code + PKCE flow with a client secret):
  ```
  http://127.0.0.1:8000/api/oauth/twitter/callback
  ```
  **Note the loopback IP, not `localhost`** — unlike every other
  platform's local redirect URI in this project, X's OAuth 2.0 authorize
  endpoint rejects `localhost` outright (see `_use_loopback_ip_for_local_host`
  above), so `127.0.0.1` is what must be registered in the Portal. The
  operator can still open the dashboard at `http://localhost:8000` in the
  browser as usual — `twitter_oauth_start` rewrites `redirect_uri` to
  `127.0.0.1` before redirecting to X regardless of which host the request
  came in on, so the two don't need to match. (Adjust the port if the
  dashboard runs elsewhere locally.) In production this needs the real
  deployed origin's equivalent
  (`https://<fly-app>.fly.dev/api/oauth/twitter/callback` or a custom
  domain) added as an additional callback URI once deployed — the
  loopback-IP rewrite only ever applies to a `localhost` host, so it's a
  no-op there.

### Phase 29c (current)
- **In-browser TikTok OAuth for client self-service** — same pattern as
  Phase 29a (YouTube) and 29b (Twitter/X), extended to TikTok. Scoped
  narrowly the same way: `role="client_user"` sessions only, always creates
  the resulting `Account` under the caller's own `client_id`. Facebook/
  Instagram still require `scripts/authorize_meta.py`. Like X, TikTok
  requires PKCE on its authorize flow — but with `scripts/authorize_tiktok.py`'s
  deliberate non-standard deviation (Phase 10): the `code_challenge` is the
  raw **hex** digest of SHA256(verifier), not RFC 7636's base64url — this
  phase reuses that exact deviation, not X's standard one.
- **`app/publishers/tiktok.py`** gained two new pure functions, mirroring
  `youtube.py`/`twitter.py`'s `build_authorization_url`/
  `exchange_code_for_credentials` shape:
  - `build_authorization_url(redirect_uri, state, code_challenge)` — builds
    TikTok's `https://www.tiktok.com/v2/auth/authorize/` consent-screen URL
    (`AUTHORIZE_URL`, `SCOPES` — unchanged from Phase 10). Reads
    `TIKTOK_CLIENT_KEY` directly from the environment (app-level, no
    `Account` row exists yet at this point in the flow — same reasoning as
    `twitter.py` reading `TWITTER_CLIENT_ID` directly). Raises
    `PermanentError` if unset.
  - `exchange_code_for_credentials(code, redirect_uri, code_verifier)` —
    reuses Phase 10's existing `exchange_authorization_code(code,
    redirect_uri, code_verifier)` outright (same `TOKEN_URL`, same
    credentials shape: `access_token`, `refresh_token`, `open_id`, `scope`,
    `expires_at`) rather than duplicating the token request. **Every
    failure normalizes to `PermanentError`** — unlike
    `exchange_authorization_code` (used directly by the CLI script, which
    lets a transient/permanent distinction pass through), this is a
    one-shot interactive flow driven by a human at the consent screen, so
    the only thing a failure needs to do is tell `dashboard/api.py`'s
    callback route to show an error and let them click "Connect" again —
    same contract as `youtube.py`'s/`twitter.py`'s
    `exchange_code_for_credentials`.
  - **The hex-digest PKCE deviation is NOT re-implemented in `tiktok.py`**
    — both functions just take/pass through whatever `code_challenge`/
    `code_verifier` they're given, same as `twitter.py`'s equivalents. The
    hex-vs-base64url choice lives entirely in the caller
    (`dashboard/api.py`, see below), matching where
    `scripts/authorize_tiktok.py`'s own `_generate_pkce_pair` already lives
    today.
- **`dashboard/api.py`**: `_generate_tiktok_pkce_pair()` (new, distinct from
  the existing `_generate_pkce_pair()` added for Twitter in Phase 29b) —
  `secrets.token_urlsafe(64)` for the verifier, **hex** SHA-256 digest
  (`hashlib.sha256(...).hexdigest()`) for the challenge, i.e. exactly
  `scripts/authorize_tiktok.py::_generate_pkce_pair`'s shape, not X's
  standard base64url one. Two new routes mirroring
  `youtube_oauth_start`/`youtube_oauth_callback` (Phase 29a) and
  `twitter_oauth_start`/`twitter_oauth_callback` (Phase 29b) exactly in
  shape:
  - `GET /api/oauth/tiktok/start` (gated, `client_user`-only) — generates
    the hex PKCE pair, signs `{"client_id", "user_id", "code_verifier"}`
    into `state` (same shape as Twitter's state, for the same reason: the
    verifier has to survive the round trip through TikTok's consent screen
    since TikTok's callback never echoes it back), and redirects to
    TikTok's authorize URL.
  - `GET /api/oauth/tiktok/callback` (public — added to `_PUBLIC_API_PATHS`
    alongside the YouTube/Twitter ones, same reasoning: TikTok's redirect
    carries no session cookie) — verifies `state`, pulls `code_verifier`
    back out of it (a validly-signed state token missing this field
    redirects with `reason=missing_pkce_verifier`, same as Twitter's
    callback), exchanges `code` + `code_verifier` for credentials, and
    upserts an `Account` row (`platform="tiktok"`, named
    `"<Client name> (self-service)"`) via the same
    `scripts/add_account.py::upsert_account` helper every other authorize
    path uses — reconnecting rotates the same Account in place instead of
    duplicating it. Always redirects back into the SPA
    (`/?screen=accounts&tiktok_connect=success` or `...=error&reason=<code>`),
    same as YouTube's/Twitter's callbacks.
- **Frontend** (`dashboard/static/index.html`): a third "Connect TikTok"
  button alongside "Connect YouTube"/"Connect X/Twitter" on the Connected
  Accounts screen (all `client_user`-only, all plain
  `<a href="/api/oauth/.../start">` navigations so the session cookie rides
  along). `state.tiktokConnectNotice` mirrors
  `state.youtubeConnectNotice`/`state.twitterConnectNotice`;
  `consumeOauthRedirectParams()` additionally reads
  `?tiktok_connect=success|error&reason=...` and strips it the same way.
- **Tests**: `tests/test_publisher_tiktok.py::TestWebOAuthFlow` (modeled on
  `tests/test_publisher_twitter.py::TestWebOAuthFlow` — missing
  `TIKTOK_CLIENT_KEY`/`_SECRET` on both functions, the authorization URL
  built with the right params, code exchange returning parsed credentials,
  a token-endpoint rejection and a transient-classified token-endpoint
  error both wrapped as `PermanentError` — since
  `exchange_code_for_credentials` normalizes either way, unlike
  `exchange_authorization_code`).
  `tests/test_dashboard_tiktok_oauth.py` (FastAPI `TestClient`, modeled on
  `tests/test_dashboard_twitter_oauth.py`): `/start` 401 anonymous / 403
  admin / redirects a `client_user` to the (mocked) TikTok URL with a
  verifiable signed state, asserting the `code_challenge` sent to TikTok
  actually derives from the `code_verifier` stashed in that state via the
  hex-digest formula (checked independently in the test, distinct from
  Twitter's base64url assertion); `/callback` handles TikTok's `error`
  param, missing code/state, a tampered state, a validly-signed state
  missing `code_verifier`, a state naming a nonexistent client, a full
  happy path asserting the created `Account`'s `client_id`/`credentials`
  (including that the right `code_verifier` reached the exchange call),
  reconnecting rotating the same `Account` instead of duplicating it, a
  simulated verifier/challenge mismatch at the token endpoint, and a
  generic exchange failure — both of the latter two leaving no `Account`
  behind.

  **Local dev setup — register this exact redirect URI in the TikTok for
  Developers portal**, per the phase brief:
  ```
  http://localhost:8000/api/oauth/tiktok/callback
  ```
  **Same caveat as Phase 10's `TIKTOK_REDIRECT_URI`: the TikTok Developer
  Portal rejects localhost/127.0.0.1 redirect URIs outright.** This route
  itself doesn't need the forwarder page the way `scripts/authorize_tiktok.py`'s
  local one-shot server does (`dashboard/api.py` is a real HTTP server, not
  a script binding a throwaway port) — but the *portal registration* still
  has to be a public HTTPS URL for local dev to work at all, the same
  GitHub-Pages-forwarder-page trick as Phase 10 (registering the forwarder's
  URL and pointing its `location.replace()` at
  `http://localhost:8000/api/oauth/tiktok/callback` instead of
  Phase 10's `:8910/callback`). This differs from YouTube's/Twitter's local
  setup (Phases 29a/29b), whose portals accept `http://localhost` directly
  — flagging it here rather than repeating their instructions verbatim,
  since copying those would silently break against TikTok's portal. Once
  deployed to a real HTTPS origin (Fly or a custom domain), the forwarder
  trick is unnecessary — register
  `https://<fly-app>.fly.dev/api/oauth/tiktok/callback` (or the custom
  domain equivalent) directly, same as every other platform's production
  redirect URI in this project. `/api/oauth/tiktok/start` derives
  `redirect_uri` from the incoming request either way, so it always matches
  whatever's actually registered as long as that's what the forwarder page
  (locally) or the browser (in production) ultimately hits.

### Phase 29d (current)
- **In-browser Facebook + Instagram OAuth for client self-service** — same
  pattern as Phase 29a/b/c, extended to Meta. Scoped narrowly the same way:
  `role="client_user"` sessions only, always creates the resulting
  Account(s) under the caller's own `client_id`. **The one structural
  difference from every prior 29x phase**: a single Meta authorization can
  yield *more than one* `Account` — one `facebook` + optionally one
  `instagram` Account *per Page* the user manages — since Meta's OAuth model
  grants access to every Page at once rather than one target account per
  authorization the way YouTube/X/TikTok do. Meta also needs no separate
  app-type credentials the way Google's Web/Desktop split does (Phase 29a)
  — the same `META_APP_ID`/`META_APP_SECRET` already in `.env` since Phase
  23 work for a browser redirect flow, only a new redirect URI needs
  registering in the Meta App Dashboard (see below). No PKCE either, unlike
  X's/TikTok's flows — Meta's OAuth dialog doesn't require it.
- **`app/publishers/meta.py`** gained one new pure function,
  `build_authorization_url(redirect_uri, state)` — mirrors
  `youtube.py`/`twitter.py`/`tiktok.py`'s `build_authorization_url` shape,
  but with no `code_challenge` parameter (no PKCE): builds Meta's
  `AUTHORIZE_URL` (the same `.../dialog/oauth` endpoint
  `scripts/authorize_meta.py` already opens in a browser) with
  `client_id`/`redirect_uri`/`state`/`scope=SCOPES`. Reads `META_APP_ID`
  (and requires `META_APP_SECRET` to also be set, via the existing
  `_app_credentials()` helper, even though the secret isn't used in this
  particular call — consistent with every other credential check in this
  module, and fails clearly before a redirect that would otherwise dead-end
  at the token exchange). **No new `exchange_code_for_credentials`
  wrapper** — unlike Phase 29a/b/c, the dashboard callback route (below)
  calls Meta's existing OAuth-chain functions directly
  (`exchange_code_for_user_token` -> `exchange_long_lived_token` ->
  `list_pages` -> `get_instagram_business_account` per Page), since a
  single exchange has to fan out into a list of Pages rather than resolve
  to one credentials dict.
- **`dashboard/api.py`**: two new routes mirroring
  `youtube_oauth_start`/`youtube_oauth_callback` (Phase 29a) in shape,
  registered as `client_user`-only / public respectively:
  - `GET /api/oauth/meta/start` — signs `{"client_id", "user_id"}` into
    `state` (no PKCE verifier, unlike Twitter's/TikTok's state — same shape
    as YouTube's) and redirects to Meta's authorize URL.
  - `GET /api/oauth/meta/callback` (public — added to `_PUBLIC_API_PATHS`,
    same reasoning as every other platform's callback: Meta's redirect
    carries no session cookie) — verifies `state`, then runs the full
    exchange chain (code -> short-lived token -> long-lived token ->
    `list_pages()`), and **for every Page returned**: upserts a `facebook`
    Account scoped to the verified `client_id`, and — only if
    `get_instagram_business_account(page_id, page_token)` finds one — also
    upserts an `instagram` Account for that same Page. **No picker**,
    unlike `scripts/authorize_meta.py`'s interactive `_choose_page` — a
    self-service `client_user` is expected to manage only their own Page(s),
    so every Page found is connected. **Account naming departs from
    Phase 29a/b/c's `"<Client name> (self-service)"`** (which assumes at
    most one Account per platform per client): here it's
    `"<Client name> - <Page name> (self-service)"`, since a client can have
    multiple Pages and `upsert_account` matches on platform+name —
    reconnecting the same Page for the same client rotates that Page's
    Account(s) in place, a genuinely new Page gets new ones. **Whole-exchange
    transaction**: every Page is processed in one loop, one `db.commit()` at
    the end — any `PublishError` (`TransientError` or `PermanentError`, from
    any step, for any Page) rolls back the whole thing and redirects with
    `reason=exchange_failed`, rather than leaving some Pages connected and
    others not from a single authorization. A `0`-Page result (user manages
    no Facebook Pages) redirects with `reason=no_pages` instead of silently
    "succeeding" with nothing connected. On success, redirects with
    `?meta_connect=success&facebook=<n>&instagram=<m>` — counts in the query
    string, since (unlike every other 29x phase) there's no single Account
    to describe.
- **Frontend** (`dashboard/static/index.html`): a single "Connect Facebook &
  Instagram" button alongside the other three on the Connected Accounts
  screen (`client_user`-only, plain `<a href="/api/oauth/meta/start">`
  navigation) — one click covers both platforms, matching Meta's actual
  OAuth model rather than offering two separate buttons. `state.metaConnectNotice`
  mirrors the other three; `consumeOauthRedirectParams()` additionally reads
  `?meta_connect=success|error&reason=...&facebook=<n>&instagram=<m>` and
  renders a summary ("Connected: 2 Facebook Pages, 1 Instagram account.") on
  success.
- **Tests**: `tests/test_publisher_meta.py::TestBuildAuthorizationUrl`
  (missing `META_APP_ID`/`_SECRET` raises `PermanentError`; the built URL
  has the right `client_id`/`state`/`redirect_uri`/scopes, no PKCE
  params). `tests/test_dashboard_meta_oauth.py` (FastAPI `TestClient`,
  modeled on `tests/test_dashboard_twitter_oauth.py` minus every PKCE-
  specific case, since Meta needs none): `/start` 401 anonymous / 403 admin
  / redirects a `client_user` to the (mocked) Meta URL with a verifiable
  signed state (`client_id` only, no `code_verifier` key); `/callback`
  handles Meta's `error` param, missing code/state, a tampered state, a
  state naming a nonexistent client, a single-Page happy path (both
  `facebook`+`instagram` Accounts created, scoped to the right `client_id`),
  a Page with no linked Instagram creating only the `facebook` Account, a
  multi-Page authorization connecting multiple distinct Accounts (verifying
  each Page gets its own distinctly-named Account and only the Page with a
  linked IG account gets an `instagram` Account), a zero-Pages result
  (`reason=no_pages`), reconnecting rotating the same Page's Account(s)
  in place rather than duplicating them, and both a `PermanentError` and a
  `TransientError` raised partway through a multi-Page loop leaving **no**
  Account behind (whole-transaction rollback, not partial).

  **Local dev setup — register this exact redirect URI in the Meta App
  Dashboard** (Facebook Login product's Valid OAuth Redirect URIs — the
  same app `META_APP_ID`/`META_APP_SECRET` already point at since Phase 23,
  no separate app-type credentials needed the way Google's Web/Desktop
  split required):
  ```
  http://localhost:8000/api/oauth/meta/callback
  ```
  (Adjust the host/port if the dashboard runs elsewhere locally —
  `/api/oauth/meta/start` derives `redirect_uri` from the incoming request,
  so it always matches whatever's actually registered as long as that's
  what's typed into the browser. Per `scripts/authorize_meta.py`'s existing
  note, Meta's App Dashboard is documented to allow `http://localhost`
  redirect URIs for an app still in Development mode — **unverified against
  a real App yet**, same caveat as Phase 23's.) In production this needs the
  real deployed origin's equivalent
  (`https://<fly-app>.fly.dev/api/oauth/meta/callback` or a custom domain)
  added as an additional Valid OAuth Redirect URI once deployed.

### Phase 30 (current)
- **Analytics screen** — the fourth scheduler-UI screen wired to a real
  backend (after Calendar/Queue in Phase 26 and the Connected-accounts
  connect buttons in Phase 29x), replacing the "isn't built yet"
  placeholder in `dashboard/static/index.html`. No schema changes — every
  number is aggregated from the existing `Job` table.
- **`GET /api/analytics/summary`** (`dashboard/api.py`) — one read-only
  endpoint, scoped **exactly like `GET /api/stats`**: an admin (Basic-Auth
  "ops" or an admin `User` session) sees everything or filters with an
  optional `?client_id=`; a `client_user` is silently confined to their own
  `client_id`, and an explicit *different* `client_id` is a `403` (same
  `_get_auth` / role-check pattern every scoped route in Phase 28 uses —
  the route takes `request: Request | None = None` so it stays directly
  callable in tests, resolving to `ADMIN_AUTH` when there's no request).
  Response (`AnalyticsSummaryOut`):
  - `total`, `published`, `failed` — job counts (`published`/`failed` are
    just the matching `by_status` entries, surfaced at top level for the
    stat cards).
  - `success_rate` — `published / (published + failed)` as a 0–1 float
    rounded to 4 dp; **`0.0` when neither a published nor a failed job
    exists** (no division, and the frontend renders `"—"` for that case
    rather than a misleading `0.0%`).
  - `by_status` — `{JobStatus value: count}` for **every** status
    (zero-filled), identical shape/semantics to `/api/stats`.
  - `by_platform` — `{platform: total count}`, only platforms that have at
    least one job.
  - `time_series` — exactly 30 entries, `{"date": "YYYY-MM-DD", "count":
    n}`, ascending, **every day present (zero-filled)** so the frontend
    bar chart has a fixed-width x-axis. Counts **jobs created per UTC day**
    (`Job.created_at`) — there is no `published_at` column, and
    `created_at` is the only stable per-job timestamp (`updated_at` moves
    on any write), so "jobs created" is the honest metric rather than an
    `updated_at`-based approximation of "published". The 30-day window is
    `_ANALYTICS_TIME_SERIES_DAYS` (a module constant). Bucketing is done
    in Python (pull `created_at` values `>= start_dt`, count into a dict)
    rather than a SQL `date()`/`date_trunc` — portable across SQLite (test
    DB) and Postgres without dialect branching, and the same
    aware-UTC-vs-naive comparison the SQL-side `>= start_dt` filter relies
    on is the one Phase 20's `dispatch_due_jobs` already established works
    on both engines.
  - `platform_breakdown` — a list (sorted by platform name) of
    `{platform, total, published, failed, last_activity}` for the design's
    per-platform table. `last_activity` is `max(Job.updated_at)` for that
    platform as an ISO string (or `null`); `_isoformat_or_none` accepts
    both a `datetime` (Postgres) and a raw ISO string (how SQLite can
    surface `func.max` over a `DateTime` column) so the endpoint doesn't
    break under the test DB.
- **Frontend** (`dashboard/static/index.html`) — `renderAnalytics()`
  replaces `renderPlaceholder("Analytics")` in the content router. Renders
  against the Phase 26 design tokens (`design_handoff_scheduler/`'s
  Analytics screen): 4 stat cards (Total jobs / Published / Failed /
  Success rate) with the blueprint corner registration marks (reusing the
  existing global `.reg-mark` classes), a zero-filled 30-bar chart of jobs
  created per day (`.bar-chart` — flat accent bars, hairline baseline, a
  sparse date axis labelling every 5th bar, `title` tooltip per bar), and
  the by-platform breakdown `data-table`. New state (`analytics` /
  `analyticsLoading` / `analyticsError`); `loadAnalytics()` fires on
  entering the screen, on client switch (`selectClient` /
  `addClientWorkspace`), and on `boot()` if the screen is restored to
  `analytics` — same wiring pattern as `loadAccounts()`. It sends
  `?client_id=` only when a workspace is active, matching `loadJobs()`.
- **Tests** — `tests/test_dashboard_analytics.py` (FastAPI `TestClient`,
  same reasoning as `tests/test_dashboard_auth.py`: the client-scoping
  behavior is middleware + route-level, not a pure helper). Covers the
  empty-data shape (30 zero days, ascending, correct first/last date;
  every `by_status` key present and 0; `success_rate == 0.0`), grouping
  (known per-platform/per-status fixture counts -> `by_platform`,
  `by_status`, `platform_breakdown` sorted with correct
  published/failed/`last_activity`, `success_rate` maths), the
  no-terminal-jobs `success_rate == 0.0` branch, time-series bucketing
  (today's bucket, a 5-day-old bucket, and a 45-day-old job correctly
  excluded), and full client scoping (`client_user` sees only their own
  numbers / `403` on another `client_id` / OK on their own; admin
  unscoped sees all, admin `?client_id=` filters). Whole suite green
  (`.venv/bin/python -m pytest -q` -> 360 passed).
- **Not built** (out of scope, follow-up candidates): a real
  published-per-day series (needs a `published_at` column or a status-
  transition audit trail — neither exists), per-day breakdown by platform
  or status (the series is a single total line), any date-range picker
  (the window is a fixed 30 days), and the design's "scheduled this week"
  / "connected accounts" stat-card variants (the four cards shipped are
  the job-outcome ones; the others would just re-expose `/api/jobs` +
  `/api/accounts` counts already visible elsewhere).

### Phase 32 (current)
- **Clients management screen** — the fifth scheduler-UI screen wired to a
  real backend (after Calendar/Queue in Phase 26, the Connected-accounts
  connect buttons in Phase 29x, and Analytics in Phase 30), replacing the
  "isn't built yet" placeholder in `dashboard/static/index.html`. Admin-only,
  in both directions: a `client_user` never sees the nav item (filtered out
  of `navItems()` the same way the Admin item is) and the screen router
  falls back to the placeholder for them, while every new/changed endpoint
  goes through `_require_admin`.
- **`app/models.py::Client.is_active`** (new column, `Boolean`,
  `nullable=False`, `default=True`) — lets an admin retire a client
  workspace without deleting it or cascading to any of its
  `Account`/`User`/`Job` rows; the flag is flipped, nothing else changes.
  Same additive-column-with-a-default shape as `Account.is_active`.
  - **Manual schema step**, same pattern as every prior additive column
    (`account_id` Phase 6, `external_id` Phase 10b, `last_stall_alert_at`
    Phase 14, the Phase 26 `client_id` columns): `init_db()`'s `create_all`
    creates whole new tables but never `ALTER`s an existing one, so on an
    existing Neon database this does **not** add `is_active` to the
    `clients` table. Run once, by hand, against Neon:
    ```sql
    ALTER TABLE clients ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE;
    ```
    The `DEFAULT TRUE` backfills every existing row in the same statement.
- **`dashboard/api.py`**:
  - `GET /api/clients` (existing admin-only route) now also returns
    `is_active` and three per-client aggregates — `account_count` (every
    `Account` for the client, active or not), `user_count` (**approved**
    `client_user`s only), `job_count` (every `Job` ever created for the
    client). Computed by `_aggregate_client_counts(db)` with **three
    grouped queries total** (one per table, `GROUP BY client_id`), not a
    per-client round trip. `ClientOut` / `ClientOut.from_client` gained the
    four fields; the counts default to `0` so `create_client` (a brand-new
    workspace) and any other caller stay valid without passing them.
  - `POST /api/clients/{id}/deactivate` and
    `POST /api/clients/{id}/reactivate` (both new, admin-only, `404` on an
    unknown id) — flip `is_active` and nothing else, returning the enriched
    `ClientOut`. No cascade: Accounts/Users/Jobs are deliberately left
    untouched (deactivation is reversible and shouldn't destroy history).
  - **A deactivated workspace blocks its `client_user`s' logins** (the
    recommended behavior from the phase brief): `login` returns a
    distinctly-worded `403` ("This client workspace has been deactivated.
    Contact your administrator.") — the same kind of small, deliberate
    no-enumeration exception already made for the pending-approval `403`.
    **It also severs an existing session**: `_resolve_auth` re-checks the
    workspace's `is_active` live (one extra `db.get(Client, ...)` by PK for
    a `client_user` token) and drops the request to `ANONYMOUS_AUTH` if the
    workspace is inactive — so deactivation takes effect on the very next
    request, not just at the next login, and every scoped route `401`s for
    them immediately. An **admin** `User` (`client_id=None`) is unaffected
    by any client's deactivation.
- **Frontend** (`dashboard/static/index.html`): `renderClients()` replaces
  `renderPlaceholder("Clients")` in the content router (guarded by
  `isAdmin()`). Grid of workspace cards against the Phase 26 design tokens
  — initial badge, name, kind (Individual/Client), an
  Accounts/Users/Jobs count strip, an Active/Inactive status pill, and a
  Deactivate (with confirm) / Reactivate button — plus a dashed "Add client
  workspace" tile that reuses the **unchanged** top-bar switcher
  `addClientWorkspace()`. New state (`clientsLoading` / `clientsError`);
  `loadClients()` (already the switcher's loader, admin-only) now tracks
  load/error state and is also fired on entering the `clients` screen and
  after a deactivate/reactivate (`setClientActive`). The "Clients" nav item
  is filtered out of `navItems()` for a `client_user` session, keeping its
  design position (last base item, before the appended Admin item) for an
  admin.
- **Tests** — `tests/test_dashboard_clients.py` (FastAPI `TestClient`, same
  reasoning as `tests/test_dashboard_auth.py`/`_analytics.py`: middleware +
  route-level behavior). Covers aggregate-count correctness (inactive
  accounts still counted, pending users excluded, client-less rows
  excluded, zeroes for an empty workspace, `create_client`'s response
  shape), deactivate/reactivate flipping the flag and not touching
  Accounts/Users/Jobs, `404` on an unknown id, admin-only enforcement
  (`client_user` `403`, anonymous `401` on both actions and the list),
  deactivated-workspace login block + clear message + login working again
  after reactivation, an existing session dropping to anonymous on
  deactivation, and an admin session being unaffected. Whole suite green
  (`.venv/bin/python -m pytest -q` -> 372 passed).
- **Not built** (out of scope, follow-up candidates): editing a client's
  name/kind, deleting a workspace, and any client-scoped self-service view
  of "my own workspace" for a `client_user` (this screen is purely the
  agency-admin roster).

### Phase 33 (current)
- **Self-service "Disconnect" on Connected Accounts** — the counterpart to
  Phase 29a-d's "Connect ..." buttons: a `client_user` can now disconnect
  one of their own connected accounts from the dashboard, no CLI/DB access
  needed.
- **`POST /api/accounts/{account_id}/disconnect`** (`dashboard/api.py`) —
  `client_user`-only (`403` for the admin path, Basic-Auth or an admin
  `User` session alike — an admin isn't scoped to any one `Client`, same
  posture as every `/api/oauth/*/start` route), and scoped to Accounts
  belonging to the caller's own `client_id` (`403` on someone else's or an
  unscoped `client_id=None` row, `404` on an unknown id). Anonymous gets
  the usual `401` from `enforce_auth` — this route isn't in
  `_PUBLIC_API_PATHS`.
  - **Deactivates and clears credentials, never deletes the row** — same
    "reversible, no cascade" posture as `deactivate_client` (Phase 32):
    sets `is_active=False` and `credentials={}` (an empty dict, not
    `None` — `Account.credentials` is a non-nullable JSON column), commits,
    and returns the updated `AccountOut` (which, as before, never
    serializes `credentials` to the browser regardless).
  - **Existing `Job` rows are untouched** — nothing here touches the `Job`
    table, so `Job.account_id`/`status`/`payload` history survives a
    disconnect exactly as-is. A job still queued against a disconnected
    Account fails clearly instead of silently using stale/cleared
    credentials, since `_resolve_account_credentials` (`app/tasks.py`,
    Phase 6) already raises `PermanentError` for `is_active=False`.
  - **Reconnecting rotates the same row in place** — no new mechanism
    needed: the Phase 29a-d OAuth callback routes already upsert by
    platform+name (`scripts/add_account.py::upsert_account`), and the
    self-service naming convention
    (`"<Client name> (self-service)"`, or
    `"<Client name> - <Page name> (self-service)"` for Meta) means clicking
    "Connect ..." again after a disconnect matches this exact row,
    overwrites `credentials`, and flips `is_active` back to `True` — the
    same account, not a duplicate.
- **Frontend** (`dashboard/static/index.html`): a "Disconnect" button next
  to each **active** connected account in `renderAccounts()`'s account-row
  list, shown only for a `client_user` session (reusing the same
  `canConnectPlatforms` check the "Connect ..." buttons already use) — an
  already-inactive account has nothing left to disconnect, so the button
  is omitted for those rows (its "Inactive" status pill already reflects
  the disconnected state). `disconnectAccount(id)` prompts
  `window.confirm(...)` before firing `POST /api/accounts/{id}/disconnect`,
  then reloads the list via `loadAccounts()`, mirroring `setClientActive`'s
  confirm-then-refresh shape (Phase 32). **No change was needed to make
  "Connect ..." available again after a disconnect** — those four buttons
  in `renderAccounts()` are unconditional (not hidden based on whether an
  account already exists for that platform), so they're already always
  clickable; disconnecting just means the next click reconnects instead of
  connecting fresh.
- **Tests** — `tests/test_dashboard_accounts_disconnect.py` (FastAPI
  `TestClient`, same reasoning as `tests/test_dashboard_clients.py`:
  ownership scoping is middleware + route-level behavior). Covers: the
  happy path (`is_active` flips, `credentials` cleared to `{}` on the DB
  row, the response never carries a `credentials` field), ownership
  enforcement (a `client_user` `403`s on another client's account and on
  an unscoped one, an admin — Basic-Auth or an admin `User` session —
  `403`s outright, anonymous `401`s, an unknown id `404`s), reconnecting
  after disconnect upserting the same `Account` row in place via
  `scripts.add_account.upsert_account` (not a duplicate), and an existing
  `Job` keeping its `account_id`/`status`/`payload` unchanged after its
  Account is disconnected. Whole suite green
  (`.venv/bin/python -m pytest -q` -> 387 passed).
- **Not built** (out of scope, follow-up candidate): letting an admin
  disconnect an account on a client's behalf (today only the owning
  `client_user` can) — would need its own `_require_admin`-style route or
  parameter, not added here since the phase brief scoped this to
  self-service only.

## Monitoring dashboard (extra, not in the spec)

- `dashboard/` — a monitoring dashboard for the engine, plus (Phase 10b)
  the TikTok webhook receiver, built without changing what `app/`, `tasks`
  and the publishers are responsible for. It only imports from `app/`
  (`SessionLocal`, `Job`, `JobStatus`, `WebhookEvent`, `publish_job`,
  `handle_tiktok_webhook_event`, `app.webhooks.tiktok`) and never the other
  way around — for the webhook route this means the same split as retry:
  the route itself does no business logic (verify signature, store the raw
  event, dispatch a task), the actual matching/status-transition/alerting
  logic lives in `app/tasks.py`, see Phase 10b above.
  - `dashboard/api.py` — FastAPI app: `GET /api/jobs` (filterable by
    `status`/`platform`, `limit` default 50, newest first), `GET
    /api/stats` (counts per `JobStatus` plus total), `POST
    /api/jobs/{id}/retry` (only for jobs in `FAILED`: resets `status` to
    `QUEUED`, `attempts` to 0, clears `error_message`, commits, then
    dispatches `publish_job.delay(id)`; returns 409 for any other status),
    and `POST /webhooks/tiktok` (Phase 10b, see above). CORS is open for
    localhost. Also serves `dashboard/static/` so the whole dashboard runs
    from a single process.
  - `dashboard/static/index.html` — single-file vanilla JS frontend (no
    build step): stat cards per status, a filterable jobs table with
    status badges and a Retry button on failed rows, auto-refreshing every
    5s.
  - Run with `uvicorn dashboard.api:app --reload --port 8000`.
  - Read-only except for the retry action and receiving TikTok webhooks.
  - Not committed yet (per instruction) — exists locally only.

## Running locally

See `README.md` for full setup and run instructions. Short version: Redis
running locally, `DATABASE_URL` set to a Neon connection string in `.env`,
then `celery -A app.celery_app worker --loglevel=info -Q priority,celery,dlq`,
`celery -A app.celery_app beat --loglevel=info`, and
`python -m scripts.enqueue_demo`.
