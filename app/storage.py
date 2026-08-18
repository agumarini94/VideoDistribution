"""
Cloudflare R2 media staging (S3-compatible object storage).

Same spirit as app/publishers/ and app/notifications.py: this module is
self-contained (no Celery imports, no prints) so it can be used and tested
independently of the job pipeline. It is NOT wired into app/tasks.py yet —
publishers will adopt it once a real media flow exists (e.g. downloading a
client-uploaded file before handing it to a platform API).

R2 credentials aren't provided by the client yet. Every function here calls
_require_config() first and raises a clear StorageNotConfiguredError instead
of letting boto3 fail cryptically (e.g. on an empty endpoint_url), and
nothing below executes at import time.

Note: the spec's "7-day media lifecycle" rule (auto-deleting staged media
after a week) is NOT implemented here — it's configured as an object
lifecycle rule on the bucket itself in the Cloudflare dashboard, not in
application code. See CLAUDE.md's Phase 3 section for the checklist.
"""

import boto3

from app.config import settings
from app.exceptions import StorageNotConfiguredError


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
