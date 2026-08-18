"""
Sends a one-off test alert so ALERT_WEBHOOK_URL can be verified
independently of a real dead-letter event.

Run it from the project root with:
    python -m scripts.test_alert

send_alert() never raises (see app/notifications.py), so success/failure is
only visible through the logging output below and, if it worked, the
message showing up in the configured Discord/Slack channel.
"""

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

from app.notifications import send_alert  # noqa: E402 - logging must be configured first


def main() -> None:
    send_alert(
        "Test alert from distribution-engine (scripts/test_alert.py). "
        "If you can see this, ALERT_WEBHOOK_URL is configured correctly."
    )


if __name__ == "__main__":
    main()
