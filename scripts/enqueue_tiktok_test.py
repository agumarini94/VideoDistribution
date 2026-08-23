"""
Creates one real tiktok job from a local video file and dispatches it —
same purpose as scripts/enqueue_youtube_test.py, adapted for TikTok's
Sandbox inbox-upload flow (Phase 10).

Unlike YouTube, TikTok has no single-account fallback (no token.json /
X_ACCESS_TOKEN equivalent — see app/publishers/tiktok.py), so --account is
required, not optional: the job is always linked to an existing Account row
(platform="tiktok"), created via scripts/authorize_tiktok.py --account.

The video lands as a draft in that account's TikTok inbox, not a live post
(Sandbox limitation — the account owner has to open the TikTok app and
manually publish it).

Always dispatches immediately (normal queue, not scheduled/urgent).

Run it from the project root with:
    python -m scripts.enqueue_tiktok_test --video /path/to/video.mp4 --account "Client X account"
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from app.db import SessionLocal, init_db
from app.models import Account, Job, JobStatus
from app.tasks import publish_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video", required=True, metavar="PATH", help="Path to the video file to upload.")
    parser.add_argument(
        "--account",
        required=True,
        metavar="NAME",
        help='Link the job to an existing Account row (platform="tiktok", this name). '
        "Required: TikTok has no single-account fallback.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    video_path = Path(args.video)
    if not video_path.is_file():
        raise SystemExit(f"Video file not found: {video_path}")

    # Idempotent: does nothing if the tables already exist.
    init_db()

    db = SessionLocal()
    try:
        account = (
            db.query(Account)
            .filter(Account.platform == "tiktok", Account.name == args.account)
            .one_or_none()
        )
        if account is None:
            raise SystemExit(
                f'No tiktok Account named {args.account!r}. '
                "Create it first with scripts/authorize_tiktok.py --account."
            )

        job = Job(
            platform="tiktok",
            payload={
                "video_path": str(video_path),
                # Unused in inbox mode (TikTok assigns no caption until the
                # user manually posts the draft from the app) — becomes
                # meaningful again once tiktok.py switches to Direct Post.
                "title": f"Distribution engine test upload {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            },
            account_id=account.id,
            status=JobStatus.QUEUED,
        )
        db.add(job)
        db.commit()

        publish_job.delay(job.id)
        print(f"Created job #{job.id} (account={args.account}) and dispatched it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
