"""
TikTok webhook verification and payload parsing (Phase 10b).

Same spirit as app/publishers/tiktok.py: this module owns all the
TikTok-specific mechanics (signature scheme, envelope shape, event
classification) so the FastAPI route in dashboard/api.py and the Celery task
in app/tasks.py stay thin — verify, parse, classify, done.

Sources (fetched while building this, since none of this can be guessed):
  - https://developers.tiktok.com/doc/webhooks-verification — signature
    header format and HMAC construction.
  - https://developers.tiktok.com/doc/webhooks-overview — delivery envelope,
    "must respond 200", "retries for up to 72h on non-2xx", "at-least-once
    delivery, so processing must be idempotent".
  - https://developers.tiktok.com/doc/webhooks-events — the only Content
    Posting-adjacent event names TikTok's docs currently list are
    "video.upload.failed" and "video.publish.completed" (envelope carries
    the identifier in a field that used to be called share_id and was
    migrated to publish_id for the Content Posting API). NOTE: this
    contradicts the event names implied by some parts of TikTok's own
    marketing copy and third-party guides (e.g. "post.publish.failed",
    "post.publish.inbox.delivered") — since we can't register a real
    Sandbox webhook yet (portal blocked, see CLAUDE.md Phase 10) to observe
    a live payload, classify_event() below matches by substring rather than
    an exhaustive hardcoded list, so it keeps working under either naming
    scheme without a code change once we can see a real event.
"""

import hashlib
import hmac
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "TikTok-Signature"

# How old a webhook's timestamp is allowed to be before it's rejected as a
# possible replay. TikTok's own verification guide flags this as something
# the receiver should decide; not specified as a fixed value by them.
_DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60


class WebhookVerificationError(Exception):
    """Raised when a request's TikTok-Signature header is missing, malformed, or doesn't match."""


class WebhookPayloadError(Exception):
    """Raised when the request body isn't a well-formed TikTok webhook envelope."""


def verification_skipped() -> bool:
    """
    True if TIKTOK_WEBHOOK_SKIP_SIGNATURE=1 — local-curl-testing-only escape
    hatch from signature verification (see dashboard/api.py, which logs a
    loud startup warning whenever this is on).
    """
    return os.getenv("TIKTOK_WEBHOOK_SKIP_SIGNATURE", "").strip() == "1"


def verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    """
    Verifies a TikTok-Signature header of the form "t=<unix_ts>,s=<hex
    hmac-sha256>". The signed message is "<unix_ts>.<raw_json_body>",
    HMAC-SHA256'd with TIKTOK_CLIENT_SECRET as the key — this uses the exact
    raw request bytes (not a re-serialized dict), since TikTok signs what it
    literally sent and re-encoding the parsed JSON is not guaranteed to
    reproduce the same bytes (key order, whitespace, unicode escaping).

    Raises WebhookVerificationError on any failure: missing/malformed
    header, secret not configured, signature mismatch, or a timestamp
    outside the tolerance window (possible replay).
    """
    if not signature_header:
        raise WebhookVerificationError(f"Missing {SIGNATURE_HEADER} header")

    parts = dict(item.split("=", 1) for item in signature_header.split(",") if "=" in item)
    timestamp_raw = parts.get("t")
    signature = parts.get("s")
    if not timestamp_raw or not signature:
        raise WebhookVerificationError(f"Malformed {SIGNATURE_HEADER} header: {signature_header!r}")

    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    if not client_secret:
        raise WebhookVerificationError("TIKTOK_CLIENT_SECRET is not configured; cannot verify webhook signatures.")

    signed_payload = f"{timestamp_raw}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(client_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookVerificationError("Signature mismatch")

    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise WebhookVerificationError(f"Non-numeric timestamp in {SIGNATURE_HEADER}: {timestamp_raw!r}") from exc

    tolerance_raw = os.getenv("TIKTOK_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", "").strip()
    tolerance = int(tolerance_raw) if tolerance_raw else _DEFAULT_TIMESTAMP_TOLERANCE_SECONDS
    age = abs(time.time() - timestamp)
    if age > tolerance:
        raise WebhookVerificationError(
            f"Webhook timestamp is {age:.0f}s old, outside the {tolerance}s tolerance (possible replay)"
        )


def parse_envelope(raw_body: bytes) -> dict:
    """
    Parses TikTok's webhook envelope:
      {"client_key": ..., "event": ..., "create_time": <epoch seconds>,
       "user_openid": ..., "content": "<JSON-encoded string>"}
    `content` is itself a JSON string, not a nested object — it has to be
    decoded a second time.

    Returns {"event_type", "create_time", "publish_id", "content_data",
    "raw_envelope"}. `publish_id` is pulled from content_data, accepting the
    legacy "share_id" key too (see the module docstring on the
    share_id -> publish_id migration) so this keeps matching either shape.

    Raises WebhookPayloadError if the body isn't JSON or is missing "event".
    """
    try:
        envelope = json.loads(raw_body)
    except ValueError as exc:
        raise WebhookPayloadError(f"Request body is not valid JSON: {exc}") from exc

    event_type = envelope.get("event")
    if not event_type:
        raise WebhookPayloadError("Webhook envelope is missing 'event'")

    content_raw = envelope.get("content")
    content_data = {}
    if content_raw:
        try:
            content_data = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
        except ValueError:
            logger.warning("TikTok webhook 'content' is not valid JSON: %r", content_raw)

    publish_id = content_data.get("publish_id") or content_data.get("share_id")

    return {
        "event_type": event_type,
        "create_time": envelope.get("create_time"),
        "publish_id": publish_id,
        "content_data": content_data,
        "raw_envelope": envelope,
    }


# Substring markers used to classify an event without an exhaustive
# hardcoded list — see the module docstring for why (event-name uncertainty
# until a real Sandbox webhook can be observed).
_FAILURE_EVENT_MARKERS = ("failed", "fail")
_SUCCESS_EVENT_MARKERS = ("completed", "delivered", "success")


def classify_event(event_type: str) -> str:
    """Returns "failure", "success", or "unknown" for a given event type string."""
    lowered = event_type.lower()
    if any(marker in lowered for marker in _FAILURE_EVENT_MARKERS):
        return "failure"
    if any(marker in lowered for marker in _SUCCESS_EVENT_MARKERS):
        return "success"
    return "unknown"
