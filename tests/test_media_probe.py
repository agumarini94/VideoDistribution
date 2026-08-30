"""
Tests for app/media_probe.py: the ffprobe subprocess wrapper. subprocess.run
and shutil.which are monkeypatched — no real ffprobe binary is invoked.
"""

import json
import subprocess

import pytest

from app import media_probe
from app.exceptions import PermanentError


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ffprobe_stdout(width=1080, height=1920, duration="30.000000"):
    return json.dumps({"streams": [{"width": width, "height": height}], "format": {"duration": duration}})


@pytest.fixture(autouse=True)
def _ffprobe_installed(monkeypatch):
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: "/usr/bin/ffprobe")


class TestProbe:
    def test_ffprobe_not_installed_raises_permanent_error(self, monkeypatch):
        monkeypatch.setattr(media_probe.shutil, "which", lambda name: None)
        with pytest.raises(PermanentError):
            media_probe.probe("video.mp4")

    def test_returns_duration_and_dimensions(self, monkeypatch):
        monkeypatch.setattr(media_probe.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout=_ffprobe_stdout()))
        result = media_probe.probe("video.mp4")
        assert result == {"duration_seconds": 30.0, "width": 1080, "height": 1920}

    def test_nonzero_exit_raises_permanent_error(self, monkeypatch):
        monkeypatch.setattr(
            media_probe.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=1, stderr="Invalid data")
        )
        with pytest.raises(PermanentError):
            media_probe.probe("corrupt.mp4")

    def test_malformed_json_output_raises_permanent_error(self, monkeypatch):
        monkeypatch.setattr(media_probe.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout="not json"))
        with pytest.raises(PermanentError):
            media_probe.probe("weird.mp4")

    def test_no_video_stream_raises_permanent_error(self, monkeypatch):
        empty = json.dumps({"streams": [], "format": {"duration": "5.0"}})
        monkeypatch.setattr(media_probe.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout=empty))
        with pytest.raises(PermanentError):
            media_probe.probe("audio_only.mp4")

    def test_subprocess_error_raises_permanent_error(self, monkeypatch):
        def _raise(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)

        monkeypatch.setattr(media_probe.subprocess, "run", _raise)
        with pytest.raises(PermanentError):
            media_probe.probe("slow.mp4")
