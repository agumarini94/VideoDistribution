"""
Alerting via Discord/Slack incoming webhooks.

Same spirit as app/publishers/: send_alert() is self-contained (no Celery
imports) and never raises. Alerting is an observability side channel, not
part of the job pipeline's correctness — a webhook outage or a missing
ALERT_WEBHOOK_URL must never stop jobs from being processed, so every
failure mode here is caught and logged instead of propagated.
"""

import logging

import requests

from app.config import settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 5


def send_alert(message: str) -> None:
    """
    Posts `message` to ALERT_WEBHOOK_URL, in whichever payload shape that
    webhook expects (Discord vs. Slack). Does nothing (besides logging a
    warning) if no webhook URL is configured.
    """
    webhook_url = settings.alert_webhook_url
    if not webhook_url:
        logger.warning("ALERT_WEBHOOK_URL is not set; skipping alert: %s", message)
        return

    payload = _build_payload(webhook_url, message)

    try:
        response = requests.post(webhook_url, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception:
        logger.exception("Failed to send alert to webhook")
        return

    logger.info("Alert sent successfully")


def _build_payload(webhook_url: str, message: str) -> dict:
    # Discord's webhook API expects {"content": ...}; it doesn't understand
    # Slack's {"text": ...} shape and rejects a body without "content".
    # Slack's incoming webhooks expect {"text": ...}. We detect Discord by
    # hostname and default to the Slack shape otherwise, since that's also
    # the format most generic/self-hosted webhook receivers expect.
    if "discord.com" in webhook_url or "discordapp.com" in webhook_url:
        return {"content": message}
    return {"text": message}
