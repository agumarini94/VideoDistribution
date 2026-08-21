"""
Creates one real youtube job from a local video file and dispatches it —
replaces the inline `python -c` blocks used to smoke-test the YouTube
publisher by hand.

Without --account: the job has no account_id, so app/publishers/youtube.py
falls back to token.json (single-account mode). With --account NAME: the
job is linked to that Account row (platform="youtube"), so the publisher
builds credentials from Account.credentials instead (Phase 7 — see
app/publishers/youtube.py and scripts/authorize_youtube.py --account).

Always creates the job as private, with a generated test title, and
dispatches it immediately (normal queue, not scheduled/urgent).

Run it from the project root with:
    python -m scripts.enqueue_youtube_test --video /path/to/video.mp4
    python -m scripts.enqueue_youtube_test --video /path/to/video.mp4 --account "Client X channel"
"""

import argparse
from datetime import datetime, timezone

from app.db import SessionLocal, init_db
from app.models import Account, Job, JobStatus
from app.tasks import publish_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video", required=True, metavar="PATH", help="Path to the video file to upload.")
    parser.add_argument(
        "--account",
        metavar="NAME",
        help='Link the job to an existing Account row (platform="youtube", this name).',
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Idempotent: does nothing if the tables already exist.
    init_db()

    db = SessionLocal()
    try:
        account_id = None
        if args.account:
            account = (
                db.query(Account)
                .filter(Account.platform == "youtube", Account.name == args.account)
                .one_or_none()
            )
            if account is None:
                raise SystemExit(
                    f'No youtube Account named {args.account!r}. '
                    "Create it first with scripts/authorize_youtube.py --account."
                )
            account_id = account.id

        job = Job(
            platform="youtube",
            payload={
                "video_path": args.video,
                "title": f"Distribution engine test upload {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                "privacy": "private",
            },
            account_id=account_id,
            status=JobStatus.QUEUED,
        )
        db.add(job)
        db.commit()

        publish_job.delay(job.id)
        print(f"Created job #{job.id} (account={args.account or '-'}) and dispatched it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
