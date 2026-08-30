"""
Video probing via ffprobe (part of ffmpeg), invoked as a subprocess.

Pure, synchronous helper with no Celery/DB knowledge — same "pure function"
spirit as app/publishers/*.py, so any publisher that needs a video's
duration/dimensions before uploading can reuse it. First user is
app/publishers/youtube.py's Shorts validation (Phase 15); app/publishers/tiktok.py
is the natural next candidate once TikTok gains its own video-shape
constraints.

Raises PermanentError, not TransientError: a missing ffmpeg install or a
file ffprobe can't read isn't something a retry fixes.
"""

import json
import shutil
import subprocess

from app.exceptions import PermanentError

_FFPROBE_ARGS = [
    "-v", "error",
    "-select_streams", "v:0",
    "-show_entries", "stream=width,height:format=duration",
    "-of", "json",
]


def probe(path: str) -> dict:
    """
    Returns {"duration_seconds": float, "width": int, "height": int} for the
    first video stream of the file at `path`.

    Raises PermanentError if: ffprobe isn't installed; the process can't be
    started or times out; ffprobe exits non-zero (unreadable/corrupt file);
    or its output can't be parsed (e.g. no video stream present).
    """
    if shutil.which("ffprobe") is None:
        raise PermanentError(
            "ffprobe not found on PATH. Install ffmpeg (which bundles ffprobe), "
            "e.g. `brew install ffmpeg`, to enable video probing."
        )

    try:
        completed = subprocess.run(
            ["ffprobe", *_FFPROBE_ARGS, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PermanentError(f"Failed to run ffprobe on {path}: {exc}") from exc

    if completed.returncode != 0:
        raise PermanentError(f"ffprobe could not read {path}: {completed.stderr.strip()}")

    try:
        data = json.loads(completed.stdout)
        stream = data["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
        duration_seconds = float(data["format"]["duration"])
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise PermanentError(f"Could not parse ffprobe output for {path}: {exc}") from exc

    return {"duration_seconds": duration_seconds, "width": width, "height": height}
