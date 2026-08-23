"""
Simulates a TikTok Content Posting API webhook POST against the locally
running dashboard (dashboard/api.py -> POST /webhooks/tiktok), for local
testing without a public HTTPS callback URL registered in the TikTok
Developer Portal — that registration is blocked until Sandbox setup /
app review goes through (see CLAUDE.md Phase 10b).

Builds a real TikTok-Signature header (t=<ts>,s=<hmac>) from
TIKTOK_CLIENT_SECRET, exercising the same verification path a real TikTok
webhook would hit (see app/webhooks/tiktok.py). Pass --skip-signature to
send no header at all, for testing the TIKTOK_WEBHOOK_SKIP_SIGNATURE=1
server-side escape hatch instead.

Run it from the project root (dashboard must be running, see README):
    python -m scripts.simulate_tiktok_webhook --scenario delivered --publish-id v_pub_url~v2.123
    python -m scripts.simulate_tiktok_webhook --scenario failed --publish-id v_pub_url~v2.123
    python -m scripts.simulate_tiktok_webhook --scenario unknown --publish-id v_pub_url~v2.123
    python -m scripts.simulate_tiktok_webhook --scenario delivered --publish-id does-not-exist

--publish-id only resolves to a Job if it matches that job's external_id
column (set automatically after a real/simulated TikTok publish succeeds,
see app/tasks.py::_persist_external_id) — pass any string to exercise the
"unknown publish_id" path instead.
"""

import argparse
import hashlib
import hmac
import json
import os
import time

import requests

import app.config  # noqa: F401 - side effect: load_dotenv(), so TIKTOK_CLIENT_SECRET etc. come from .env

# Event names per developers.tiktok.com/doc/webhooks-events (the only
# Content Posting-adjacent ones TikTok's docs currently list) — see the
# naming caveat in app/webhooks/tiktok.py's module docstring.
_SCENARIOS = {
    "delivered": {
        "event": "video.publish.completed",
        "content": lambda publish_id: {"publish_id": publish_id},
    },
    "failed": {
        "event": "video.upload.failed",
        "content": lambda publish_id: {"publish_id": publish_id, "fail_reason": "video_format_check_failed"},
    },
    "unknown": {
        # Not a documented event name — exercises classify_event()'s
        # "unknown" branch (audit-logged, no job status change).
        "event": "video.publish.pending_review",
        "content": lambda publish_id: {"publish_id": publish_id},
    },
}


def _sign(raw_body: bytes, client_secret: str, timestamp: int) -> str:
    signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(client_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},s={digest}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=sorted(_SCENARIOS), default="delivered")
    parser.add_argument(
        "--publish-id",
        default="v_pub_url~v2.SIMULATED123",
        help="Matched against Job.external_id; use an id from a real/simulated job to see it resolve.",
    )
    parser.add_argument("--url", default="http://localhost:8000/webhooks/tiktok")
    parser.add_argument(
        "--skip-signature",
        action="store_true",
        help="Send no TikTok-Signature header (server must have TIKTOK_WEBHOOK_SKIP_SIGNATURE=1 set).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scenario = _SCENARIOS[args.scenario]
    envelope = {
        "client_key": os.getenv("TIKTOK_CLIENT_KEY", "simulated_client_key"),
        "event": scenario["event"],
        "create_time": int(time.time()),
        "user_openid": "simulated_open_id",
        "content": json.dumps(scenario["content"](args.publish_id)),
    }
    raw_body = json.dumps(envelope).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if not args.skip_signature:
        client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
        if not client_secret:
            print(
                "TIKTOK_CLIENT_SECRET is not set in .env — either set it (to sign like a real "
                "webhook) or pass --skip-signature together with TIKTOK_WEBHOOK_SKIP_SIGNATURE=1 "
                "on the server."
            )
            return
        headers["TikTok-Signature"] = _sign(raw_body, client_secret, int(time.time()))

    response = requests.post(args.url, data=raw_body, headers=headers, timeout=10)
    print(f"POST {args.url} -> HTTP {response.status_code}")
    print(response.text)


if __name__ == "__main__":
    main()
