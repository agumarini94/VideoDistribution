#!/bin/bash
# Single-machine Fly.io entrypoint (see fly.toml / CLAUDE.md Phase 9): the
# NEW JOB upload flow (dashboard/api.py::create_job) writes files to local
# disk under uploads/, so the worker that later reads that path and the api
# process that wrote it must share a filesystem — meaning they must run on
# the same machine, not as separate Fly processes. Split back into
# separate worker/beat/api processes once R2 storage is wired into the
# upload flow and uploads no longer live on local disk.
set -e

echo "start_all: starting celery worker (queues: priority,celery,dlq)"
celery -A app.celery_app worker --loglevel=info -Q priority,celery,dlq &

echo "start_all: starting celery beat"
celery -A app.celery_app beat --loglevel=info &

echo "start_all: starting dashboard api (foreground)"
exec uvicorn dashboard.api:app --host 0.0.0.0 --port 8000
