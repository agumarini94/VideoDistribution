"""
Creates one real twitter job (single tweet, image, video, or thread) and
dispatches it — smoke-tests app/publishers/twitter.py end-to-end (Phase 21:
OAuth 2.0 + X API v2) the same way scripts/enqueue_youtube_test.py does for
YouTube.

Without --account: the job has no account_id, so app/publishers/twitter.py
falls back to TWITTER_CLIENT_ID / TWITTER_CLIENT_SECRET / TWITTER_ACCESS_TOKEN
/ TWITTER_REFRESH_TOKEN env vars (single-account mode). Note (Phase 21): X's
refresh tokens are single-use and rotate on every refresh, and single-account
mode has no Account row to persist a rotated refresh_token onto — it only
survives one reactive refresh before the env var goes stale. Prefer
--account for anything beyond a one-off smoke test; create the Account row
first with scripts/add_account.py.

With --account NAME: the job is linked to that Account row
(platform="twitter"), so the publisher and the token-refresh flow
(app/tasks.py) use Account.credentials, including token rotation.

Run it from the project root with:
    python -m scripts.enqueue_twitter_test --mode text
    python -m scripts.enqueue_twitter_test --mode image --file photo.jpg --account "Main account"
    python -m scripts.enqueue_twitter_test --mode video --file clip.mp4 --account "Main account"
    python -m scripts.enqueue_twitter_test --mode thread --account "Main account"
"""

import argparse
from datetime import datetime, timezone

from app.db import SessionLocal, init_db
from app.models import Account, Job, JobStatus
from app.tasks import publish_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", required=True, choices=["text", "image", "video", "thread"], help="What kind of job to create.")
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="Local media file path. Required for --mode image/video; optional first-tweet attachment for --mode thread.",
    )
    parser.add_argument(
        "--account",
        metavar="NAME",
        help='Link the job to an existing Account row (platform="twitter", this name).',
    )
    return parser.parse_args()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_payload(args: argparse.Namespace) -> dict:
    if args.mode == "text":
        return {"text": f"Distribution engine test tweet {_timestamp()}"}

    if args.mode in ("image", "video"):
        if not args.file:
            raise SystemExit(f"--file is required for --mode {args.mode}.")
        return {"text": f"Distribution engine test tweet ({args.mode}) {_timestamp()}", "media_paths": [args.file]}

    # thread: three chained tweets; the optional --file attaches to the first.
    stamp = _timestamp()
    tweets = [{"text": f"Distribution engine test thread {i + 1}/3 {stamp}"} for i in range(3)]
    if args.file:
        tweets[0]["media_paths"] = [args.file]
    return {"thread": tweets}


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
                .filter(Account.platform == "twitter", Account.name == args.account)
                .one_or_none()
            )
            if account is None:
                raise SystemExit(
                    f"No twitter Account named {args.account!r}. "
                    "Create it first with scripts/add_account.py --platform twitter --name ..."
                )
            account_id = account.id

        job = Job(
            platform="twitter",
            payload=_build_payload(args),
            account_id=account_id,
            status=JobStatus.QUEUED,
        )
        db.add(job)
        db.commit()

        publish_job.delay(job.id)
        print(f"Created job #{job.id} (mode={args.mode}, account={args.account or '-'}) and dispatched it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
