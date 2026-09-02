"""
Creates one real facebook job (text, photo, or video post to a Page) and
dispatches it — smoke-tests app/publishers/facebook.py end-to-end (Phase 24)
the same way scripts/enqueue_twitter_test.py does for Twitter.

Unlike Twitter/YouTube, Facebook has no single-account/env-var fallback
(Phase 23's decision): --account is always required, and the Account must
already exist (create it with scripts/authorize_meta.py, which links a Page
and stores its page_id/page_token).

Run it from the project root with:
    python -m scripts.enqueue_facebook_test --mode text --account "Main Page"
    python -m scripts.enqueue_facebook_test --mode photo --file photo.jpg --account "Main Page"
    python -m scripts.enqueue_facebook_test --mode video --file clip.mp4 --account "Main Page"
"""

import argparse
from datetime import datetime, timezone

from app.db import SessionLocal, init_db
from app.models import Account, Job, JobStatus
from app.tasks import publish_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", required=True, choices=["text", "photo", "video"], help="What kind of job to create.")
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="Local media file path. Required for --mode photo/video.",
    )
    parser.add_argument(
        "--account",
        required=True,
        metavar="NAME",
        help='Link the job to an existing Account row (platform="facebook", this name). Required — no single-account fallback.',
    )
    return parser.parse_args()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_payload(args: argparse.Namespace) -> dict:
    if args.mode == "text":
        return {"text": f"Distribution engine test post {_timestamp()}"}

    if not args.file:
        raise SystemExit(f"--file is required for --mode {args.mode}.")

    payload = {"text": f"Distribution engine test post ({args.mode}) {_timestamp()}", "media_paths": [args.file]}
    if args.mode == "video":
        payload["title"] = f"Distribution engine test video {_timestamp()}"
    return payload


def main() -> None:
    args = parse_args()

    # Idempotent: does nothing if the tables already exist.
    init_db()

    db = SessionLocal()
    try:
        account = (
            db.query(Account)
            .filter(Account.platform == "facebook", Account.name == args.account)
            .one_or_none()
        )
        if account is None:
            raise SystemExit(
                f"No facebook Account named {args.account!r}. "
                "Create it first with scripts/authorize_meta.py."
            )

        job = Job(
            platform="facebook",
            payload=_build_payload(args),
            account_id=account.id,
            status=JobStatus.QUEUED,
        )
        db.add(job)
        db.commit()

        publish_job.delay(job.id)
        print(f"Created job #{job.id} (mode={args.mode}, account={args.account}) and dispatched it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
