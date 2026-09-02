"""
Tests for the R2 media-staging wired into the dashboard's NEW JOB flow
(Phase 22, dashboard/api.py::_stage_to_r2 and create_job). storage.upload_file
is monkeypatched (no real R2/network call); publish_job.delay is monkeypatched
the same way tests/test_tasks_dispatch_due_jobs.py does, so no Celery broker
is needed.

create_job is a plain async function under FastAPI's Form(...)/File(...)
defaults — calling it directly (bypassing the HTTP layer/dependency
injection) with real values and a real UploadFile is enough to exercise its
body, same spirit as every other test in this suite calling task functions
directly instead of going through Celery.
"""

import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile

from dashboard import api as dashboard_api
from app.exceptions import StorageNotConfiguredError
from app.models import Account, Job


def _upload_file(filename: str, content: bytes = b"fake file bytes") -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=filename)


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard_api.publish_job, "delay", lambda job_id: calls.append(job_id))
    return calls


def _run_create_job(db_session, **overrides):
    kwargs = dict(
        platform="youtube",
        file=None,
        media_files=[],
        account_id=None,
        title=None,
        text=None,
        privacy=None,
        shorts=False,
        playlist_id=None,
        db=db_session,
    )
    kwargs.update(overrides)
    return asyncio.run(dashboard_api.create_job(**kwargs))


class TestStageToR2:
    def test_returns_public_url_on_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            dashboard_api.storage,
            "upload_file",
            lambda path: {"key": "2026/09/02/abc.mp4", "public_url": "https://pub.example/2026/09/02/abc.mp4"},
        )
        local_file = tmp_path / "video.mp4"
        local_file.write_bytes(b"x")

        assert dashboard_api._stage_to_r2(local_file) == "https://pub.example/2026/09/02/abc.mp4"

    def test_returns_none_when_not_configured(self, monkeypatch, tmp_path):
        def _raise(path):
            raise StorageNotConfiguredError("R2 not configured")

        monkeypatch.setattr(dashboard_api.storage, "upload_file", _raise)
        local_file = tmp_path / "video.mp4"
        local_file.write_bytes(b"x")

        assert dashboard_api._stage_to_r2(local_file) is None

    def test_returns_none_on_unexpected_error_without_raising(self, monkeypatch, tmp_path):
        def _raise(path):
            raise RuntimeError("network blip")

        monkeypatch.setattr(dashboard_api.storage, "upload_file", _raise)
        local_file = tmp_path / "video.mp4"
        local_file.write_bytes(b"x")

        assert dashboard_api._stage_to_r2(local_file) is None


class TestCreateJobR2Integration:
    def test_youtube_upload_attaches_media_public_url(self, db_session, dispatched, monkeypatch):
        monkeypatch.setattr(
            dashboard_api.storage,
            "upload_file",
            lambda path: {"key": "2026/09/02/abc.mp4", "public_url": "https://pub.example/2026/09/02/abc.mp4"},
        )

        result = _run_create_job(db_session, platform="youtube", file=_upload_file("video.mp4"))

        job = db_session.get(Job, result.id)
        assert job.payload["media_public_url"] == "https://pub.example/2026/09/02/abc.mp4"
        assert "video_path" in job.payload
        assert dispatched == [job.id]

    def test_youtube_upload_degrades_gracefully_when_r2_not_configured(self, db_session, dispatched, monkeypatch):
        def _raise(path):
            raise StorageNotConfiguredError("R2 not configured")

        monkeypatch.setattr(dashboard_api.storage, "upload_file", _raise)

        result = _run_create_job(db_session, platform="youtube", file=_upload_file("video.mp4"))

        job = db_session.get(Job, result.id)
        assert "media_public_url" not in job.payload
        assert job.payload["video_path"]
        assert dispatched == [job.id]

    def test_youtube_upload_degrades_gracefully_on_unexpected_storage_error(self, db_session, dispatched, monkeypatch):
        def _raise(path):
            raise RuntimeError("boom")

        monkeypatch.setattr(dashboard_api.storage, "upload_file", _raise)

        result = _run_create_job(db_session, platform="youtube", file=_upload_file("video.mp4"))

        job = db_session.get(Job, result.id)
        assert "media_public_url" not in job.payload
        assert dispatched == [job.id]

    def test_twitter_media_public_urls_attached_when_all_succeed(self, db_session, dispatched, monkeypatch):
        monkeypatch.setattr(
            dashboard_api.storage,
            "upload_file",
            lambda path: {"key": "k", "public_url": f"https://pub.example/{path}"},
        )

        result = _run_create_job(
            db_session,
            platform="twitter",
            text="hello world",
            media_files=[_upload_file("a.png"), _upload_file("b.png")],
        )

        job = db_session.get(Job, result.id)
        assert len(job.payload["media_public_urls"]) == 2
        assert len(job.payload["media_paths"]) == 2

    def test_twitter_media_public_urls_omitted_on_partial_failure(self, db_session, dispatched, monkeypatch):
        calls = {"n": 0}

        def _flaky(path):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"key": "k", "public_url": "https://pub.example/a.png"}
            raise RuntimeError("boom")

        monkeypatch.setattr(dashboard_api.storage, "upload_file", _flaky)

        result = _run_create_job(
            db_session,
            platform="twitter",
            text="hello world",
            media_files=[_upload_file("a.png"), _upload_file("b.png")],
        )

        job = db_session.get(Job, result.id)
        assert "media_public_urls" not in job.payload
        assert len(job.payload["media_paths"]) == 2

    def _make_instagram_account(self, db_session):
        account = Account(platform="instagram", name="Main Page", credentials={}, is_active=True)
        db_session.add(account)
        db_session.commit()
        db_session.refresh(account)
        return account

    def test_instagram_attaches_media_public_url_and_no_local_path(self, db_session, dispatched, monkeypatch):
        monkeypatch.setattr(
            dashboard_api.storage,
            "upload_file",
            lambda path: {"key": "k", "public_url": "https://pub.example/2026/09/02/abc.jpg"},
        )
        account = self._make_instagram_account(db_session)

        result = _run_create_job(
            db_session,
            platform="instagram",
            account_id=account.id,
            text="a caption",
            media_files=[_upload_file("photo.jpg")],
        )

        job = db_session.get(Job, result.id)
        assert job.payload["media_public_url"] == "https://pub.example/2026/09/02/abc.jpg"
        assert job.payload["text"] == "a caption"
        assert "media_paths" not in job.payload
        assert dispatched == [job.id]

    def test_instagram_rejects_job_when_r2_not_configured(self, db_session, dispatched, monkeypatch):
        def _raise(path):
            raise StorageNotConfiguredError("R2 not configured")

        monkeypatch.setattr(dashboard_api.storage, "upload_file", _raise)
        account = self._make_instagram_account(db_session)

        with pytest.raises(HTTPException) as excinfo:
            _run_create_job(
                db_session,
                platform="instagram",
                account_id=account.id,
                media_files=[_upload_file("photo.jpg")],
            )

        assert excinfo.value.status_code == 400
        assert "R2" in excinfo.value.detail
        assert dispatched == []

    def test_instagram_without_media_file_is_rejected(self, db_session, dispatched):
        account = self._make_instagram_account(db_session)

        with pytest.raises(HTTPException) as excinfo:
            _run_create_job(db_session, platform="instagram", account_id=account.id, text="no media")

        assert excinfo.value.status_code == 400
        assert dispatched == []
