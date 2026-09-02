"""
Cloudflare R2 media staging (S3-compatible object storage).

Same spirit as app/publishers/ and app/notifications.py: this module is
self-contained (no Celery imports, no prints) so it can be used and tested
independently of the job pipeline.

R2 is wired into the dashboard's NEW JOB upload flow (Phase 22,
dashboard/api.py) as a best-effort side upload: the local file stays the
source of truth for every publisher today, R2 just also gets a copy so its
public_url is available for the upcoming Instagram publisher (which needs a
public URL, not a local path) and as groundwork for the Fly deploy (where
local disk won't be shared across processes). Every function here calls
_require_config() first and raises a clear StorageNotConfiguredError instead
of letting boto3 fail cryptically (e.g. on an empty endpoint_url), and
nothing below executes at import time.

Note: the spec's "7-day media lifecycle" rule (auto-deleting staged media
after a week) is NOT implemented here — it's configured as an object
lifecycle rule on the bucket itself in the Cloudflare dashboard, not in
application code. See CLAUDE.md's Phase 3 section for the checklist.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3

from app.config import settings
from app.exceptions import StorageNotConfiguredError


def upload_file(local_path: str) -> dict:
    """
    Uploads the file at `local_path` to the bucket under a collision-safe
    key (UTC date + a random uuid segment, keeping the original extension)
    and returns {"key": ..., "public_url": ...}.

    public_url is R2_PUBLIC_BASE_URL + "/" + key — the bucket's public
    r2.dev base URL (or a custom domain, if one is ever attached) — for
    callers that need a plain public URL rather than a presigned one (see
    module docstring). Raises StorageNotConfiguredError if R2_PUBLIC_BASE_URL,
    or any of the other R2_* vars generate_signed_url/delete_media need, is
    missing.
    """
    _require_config()
    if not settings.r2_public_base_url:
        raise StorageNotConfiguredError(
            "Cloudflare R2 is not configured yet (missing: R2_PUBLIC_BASE_URL). "
            "Set this in .env once the client provides the bucket's public "
            "r2.dev base URL — see .env.example."
        )

    key = _build_key(local_path)
    client = _get_client()
    client.upload_file(local_path, settings.r2_bucket_name, key)
    public_url = f"{settings.r2_public_base_url.rstrip('/')}/{key}"
    return {"key": key, "public_url": public_url}


def _build_key(local_path: str) -> str:
    date_prefix = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    suffix = Path(local_path).suffix
    return f"{date_prefix}/{uuid.uuid4().hex}{suffix}"


def upload_media(local_path: str, key: str) -> None:
    """Uploads the file at `local_path` to the bucket under `key`."""
    client = _get_client()
    client.upload_file(local_path, settings.r2_bucket_name, key)


def generate_signed_url(key: str, expires_seconds: int = 3600) -> str:
    """
    Returns a presigned GET URL for `key`, valid for `expires_seconds`.

    This is what platform APIs use to ingest media directly from R2,
    per the spec, instead of routing the file bytes through this service.
    """
    client = _get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket_name, "Key": key},
        ExpiresIn=expires_seconds,
    )


def delete_media(key: str) -> None:
    """Removes `key` from the bucket."""
    client = _get_client()
    client.delete_object(Bucket=settings.r2_bucket_name, Key=key)


def _get_client():
    _require_config()
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def _require_config() -> None:
    missing = [
        name
        for name, value in (
            ("R2_ENDPOINT_URL", settings.r2_endpoint_url),
            ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
            ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
            ("R2_BUCKET_NAME", settings.r2_bucket_name),
        )
        if not value
    ]
    if missing:
        raise StorageNotConfiguredError(
            "Cloudflare R2 is not configured yet (missing: "
            f"{', '.join(missing)}). Set these in .env once the client "
            "provides R2 credentials — see .env.example."
        )
