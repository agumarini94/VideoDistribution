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

RUN chown -R appuser:appuser /app
USER appuser

# Default process is the Celery worker. Beat and the dashboard API run the
# same image with a different command (see docker-compose.yml / fly.toml).
CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=info", "-Q", "priority,celery,dlq"]
