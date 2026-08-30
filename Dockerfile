FROM python:3.13-slim

# All configuration comes from environment variables (see app/config.py) —
# no secrets are baked into this image. Set them via `docker run -e`,
# `env_file` in docker-compose, or `fly secrets` in production.

RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

# Copy requirements first so `pip install` is only re-run when dependencies
# actually change, not on every code edit (Docker layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x scripts/start_all.sh

RUN chown -R appuser:appuser /app
USER appuser

# Default process runs worker + beat (background) + the dashboard api
# (foreground) in a single container — see scripts/start_all.sh for why
# (uploads/ is local disk, so worker and api must share a filesystem; see
# CLAUDE.md Phase 9). docker-compose.yml overrides this per-service since
# it doesn't have that constraint locally.
CMD ["scripts/start_all.sh"]
