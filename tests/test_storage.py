"""
Tests for app/storage.py (Phase 22: R2 wiring). boto3.client is
monkeypatched with an in-memory fake — no real network call is made, no
real R2 credentials are needed.
"""

from types import SimpleNamespace

import pytest

from app import storage
from app.exceptions import StorageNotConfiguredError


def _fake_settings(**overrides):
    base = dict(
        r2_endpoint_url="https://account.r2.cloudflarestorage.com",
        r2_access_key_id="test-access-key",
        r2_secret_access_key="test-secret-key",
        r2_bucket_name="test-bucket",
        r2_public_base_url="https://pub-test.r2.dev",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeS3Client:
    def __init__(self):
        self.uploaded = []
        self.deleted = []

    def upload_file(self, local_path, bucket, key):
        self.uploaded.append((local_path, bucket, key))

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://signed.example/{Params['Key']}?expires={ExpiresIn}"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(storage, "settings", _fake_settings())

    fake_client = _FakeS3Client()
    monkeypatch.setattr(storage.boto3, "client", lambda *a, **k: fake_client)
    return fake_client


class TestUploadFile:
    def test_uploads_and_returns_key_and_public_url(self, configured, tmp_path):
        local_file = tmp_path / "video.mp4"
        local_file.write_bytes(b"fake video bytes")

        result = storage.upload_file(str(local_file))

        assert result["key"].endswith(".mp4")
        assert result["public_url"] == f"https://pub-test.r2.dev/{result['key']}"
        assert configured.uploaded == [(str(local_file), "test-bucket", result["key"])]

    def test_key_is_namespaced_by_date_and_random(self, configured, tmp_path):
        local_file = tmp_path / "a.png"
        local_file.write_bytes(b"x")

        from datetime import datetime, timezone

        result_a = storage.upload_file(str(local_file))
        result_b = storage.upload_file(str(local_file))

        today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        assert result_a["key"].startswith(f"{today}/")
        assert result_a["key"] != result_b["key"]

    def test_missing_r2_public_base_url_raises(self, configured, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "settings", _fake_settings(r2_public_base_url=""))
        local_file = tmp_path / "video.mp4"
        local_file.write_bytes(b"x")

        with pytest.raises(StorageNotConfiguredError, match="R2_PUBLIC_BASE_URL"):
            storage.upload_file(str(local_file))

    @pytest.mark.parametrize(
        "missing_attr,missing_env_name",
        [
            ("r2_endpoint_url", "R2_ENDPOINT_URL"),
            ("r2_access_key_id", "R2_ACCESS_KEY_ID"),
            ("r2_secret_access_key", "R2_SECRET_ACCESS_KEY"),
            ("r2_bucket_name", "R2_BUCKET_NAME"),
        ],
    )
    def test_missing_core_r2_config_raises(self, configured, tmp_path, monkeypatch, missing_attr, missing_env_name):
        monkeypatch.setattr(storage, "settings", _fake_settings(**{missing_attr: ""}))
        local_file = tmp_path / "video.mp4"
        local_file.write_bytes(b"x")

        with pytest.raises(StorageNotConfiguredError, match=missing_env_name):
            storage.upload_file(str(local_file))

    def test_no_config_at_all_raises_before_any_boto3_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            storage,
            "settings",
            _fake_settings(
                r2_endpoint_url="",
                r2_access_key_id="",
                r2_secret_access_key="",
                r2_bucket_name="",
                r2_public_base_url="",
            ),
        )

        def _boom(*a, **k):
            raise AssertionError("boto3.client should not be called when R2 isn't configured")

        monkeypatch.setattr(storage.boto3, "client", _boom)
        local_file = tmp_path / "video.mp4"
        local_file.write_bytes(b"x")

        with pytest.raises(StorageNotConfiguredError):
            storage.upload_file(str(local_file))


class TestUploadMediaGenerateSignedUrlDeleteMedia:
    def test_upload_media_uses_configured_bucket(self, configured, tmp_path):
        local_file = tmp_path / "x.txt"
        local_file.write_bytes(b"x")

        storage.upload_media(str(local_file), "some/key.txt")

        assert configured.uploaded == [(str(local_file), "test-bucket", "some/key.txt")]

    def test_generate_signed_url_returns_presigned_url(self, configured):
        url = storage.generate_signed_url("some/key.txt", expires_seconds=120)
        assert url == "https://signed.example/some/key.txt?expires=120"

    def test_delete_media_deletes_from_configured_bucket(self, configured):
        storage.delete_media("some/key.txt")
        assert configured.deleted == [("test-bucket", "some/key.txt")]

    def test_raises_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(
            storage,
            "settings",
            _fake_settings(r2_endpoint_url="", r2_access_key_id="", r2_secret_access_key="", r2_bucket_name=""),
        )

        with pytest.raises(StorageNotConfiguredError):
            storage.upload_media("local.txt", "key.txt")
