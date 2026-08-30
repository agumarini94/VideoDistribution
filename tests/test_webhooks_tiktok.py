"""
Tests for app/webhooks/tiktok.py: signature verification, envelope parsing,
and event classification. No network calls, no DB — this module is pure.
"""

import hashlib
import hmac
import json
import time

import pytest

from app.webhooks import tiktok as webhook

_SECRET = "test-client-secret"


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    signed_payload = f"{timestamp}.{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},s={digest}"


@pytest.fixture(autouse=True)
def _tiktok_secret(monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", _SECRET)
    monkeypatch.delenv("TIKTOK_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", raising=False)


class TestVerifySignature:
    def test_valid_signature_does_not_raise(self):
        body = b'{"event": "video.publish.completed"}'
        header = _sign(_SECRET, int(time.time()), body)
        webhook.verify_signature(body, header)

    def test_wrong_signature_raises(self):
        body = b'{"event": "video.publish.completed"}'
        header = f"t={int(time.time())},s=" + "0" * 64
        with pytest.raises(webhook.WebhookVerificationError):
            webhook.verify_signature(body, header)

    def test_tampered_body_raises(self):
        # Signature computed over one body, but a different body is verified
        # against it — same as an attacker (or a proxy) mutating the payload
        # after TikTok signed it.
        body = b'{"event": "video.publish.completed"}'
        header = _sign(_SECRET, int(time.time()), body)
        with pytest.raises(webhook.WebhookVerificationError):
            webhook.verify_signature(b'{"event": "video.upload.failed"}', header)

    def test_expired_timestamp_raises(self):
        body = b'{"event": "video.publish.completed"}'
        old_timestamp = int(time.time()) - 10_000  # well outside the 300s default tolerance
        header = _sign(_SECRET, old_timestamp, body)
        with pytest.raises(webhook.WebhookVerificationError):
            webhook.verify_signature(body, header)

    def test_timestamp_within_custom_tolerance_passes(self, monkeypatch):
        monkeypatch.setenv("TIKTOK_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", "3600")
        body = b'{"event": "video.publish.completed"}'
        old_timestamp = int(time.time()) - 1800  # 30 min old, within the 1h override
        header = _sign(_SECRET, old_timestamp, body)
        webhook.verify_signature(body, header)

    def test_missing_header_raises(self):
        with pytest.raises(webhook.WebhookVerificationError):
            webhook.verify_signature(b"{}", None)

    @pytest.mark.parametrize(
        "header",
        ["", "garbage", "t=123", "s=" + "0" * 64, "t=,s=", "t=notanumber,s=" + "0" * 64],
        ids=["empty", "no-equals", "missing-s", "missing-t", "both-empty", "non-numeric-t"],
    )
    def test_malformed_header_raises(self, header):
        with pytest.raises(webhook.WebhookVerificationError):
            webhook.verify_signature(b"{}", header)

    def test_missing_client_secret_raises(self, monkeypatch):
        monkeypatch.delenv("TIKTOK_CLIENT_SECRET", raising=False)
        body = b"{}"
        header = f"t={int(time.time())},s=" + "0" * 64
        with pytest.raises(webhook.WebhookVerificationError):
            webhook.verify_signature(body, header)


class TestParseEnvelope:
    def test_valid_envelope_with_publish_id(self):
        content = json.dumps({"publish_id": "v_pub_url~v2.123"})
        body = json.dumps(
            {"event": "video.publish.completed", "create_time": 1735689600, "content": content}
        ).encode()

        result = webhook.parse_envelope(body)

        assert result["event_type"] == "video.publish.completed"
        assert result["create_time"] == 1735689600
        assert result["publish_id"] == "v_pub_url~v2.123"
        assert result["content_data"] == {"publish_id": "v_pub_url~v2.123"}
        assert result["raw_envelope"]["event"] == "video.publish.completed"

    def test_legacy_share_id_key_is_accepted(self):
        content = json.dumps({"share_id": "legacy-id-123"})
        body = json.dumps({"event": "video.publish.completed", "content": content}).encode()

        result = webhook.parse_envelope(body)

        assert result["publish_id"] == "legacy-id-123"

    def test_missing_content_yields_no_publish_id(self):
        body = json.dumps({"event": "video.publish.completed"}).encode()

        result = webhook.parse_envelope(body)

        assert result["content_data"] == {}
        assert result["publish_id"] is None

    def test_invalid_json_body_raises(self):
        with pytest.raises(webhook.WebhookPayloadError):
            webhook.parse_envelope(b"not json at all")

    def test_missing_event_raises(self):
        body = json.dumps({"content": "{}"}).encode()
        with pytest.raises(webhook.WebhookPayloadError):
            webhook.parse_envelope(body)

    def test_malformed_content_json_is_swallowed_not_raised(self):
        # `content` is present but isn't valid JSON: parse_envelope logs a
        # warning and treats it as empty rather than failing the whole
        # request over a field TikTok itself may have sent malformed.
        body = json.dumps({"event": "video.publish.completed", "content": "not-json"}).encode()

        result = webhook.parse_envelope(body)

        assert result["content_data"] == {}
        assert result["publish_id"] is None


class TestClassifyEvent:
    @pytest.mark.parametrize(
        "event_type",
        ["video.upload.failed", "post.publish.failed", "some.FAIL.event"],
    )
    def test_failure_markers(self, event_type):
        assert webhook.classify_event(event_type) == "failure"

    @pytest.mark.parametrize(
        "event_type",
        ["video.publish.completed", "post.publish.inbox.delivered", "some.SUCCESS.event"],
    )
    def test_success_markers(self, event_type):
        assert webhook.classify_event(event_type) == "success"

    def test_unrecognized_event_is_unknown(self):
        assert webhook.classify_event("authorization.removed") == "unknown"
