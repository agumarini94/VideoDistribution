"""
Creates one real pinterest job (an image or video Pin) and dispatches it —
smoke-tests app/publishers/pinterest.py end-to-end (Phase 29e) the same way
scripts/enqueue_facebook_test.py/enqueue_instagram_test.py do for their
platforms.

Unlike every other enqueue_*_test.py script, this one's media handling
depends on --mode: for --mode image it uploads the given file to Cloudflare
R2 itself (same reason scripts/enqueue_instagram_test.py does — Pinterest
image Pins read media_public_url, not a local path) and will fail loudly if
R2 isn't configured; for --mode video it just passes the local file path
through as media_paths, since app/publishers/pinterest.py uploads the raw
video file itself via Pinterest's own 3-step upload flow.

Like Facebook/Instagram, Pinterest has no single-account/env-var fallback:
--account is always required, and the Account must already exist — connect
one via the dashboard's self-service "Connect Pinterest" button
(client_user session), or scripts/add_account.py by hand.

Run it from the project root with:
    python -m scripts.enqueue_pinterest_test --mode image --file photo.jpg --account "Main account" --board-id 123456789012345678
    python -m scripts.enqueue_pinterest_test --mode video --file clip.mp4 --account "Main account" --board-id 123456789012345678
"""

import argparse
from datetime import datetime, timezone

from app.db import SessionLocal, init_db
from app.models import Account, Job, JobStatus
from app.storage import upload_file
from app.tasks import publish_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--mode",
        required=True,
        choices=["image", "video"],
        help="What kind of Pin this is — decides whether --file is staged to R2 (image) or passed as a local path (video).",
    )
    parser.add_argument("--file", required=True, metavar="PATH", help="Local media file.")
    parser.add_argument(
        "--account",
        required=True,
        metavar="NAME",
        help='Link the job to an existing Account row (platform="pinterest", this name). Required — no single-account fallback.',
    )
    parser.add_argument("--board-id", required=True, metavar="BOARD_ID", help="Destination Pinterest board id.")
    parser.add_argument("--title", metavar="TEXT", help="Pin title (required by Pinterest). Auto-generated if omitted.")
    parser.add_argument("--description", metavar="TEXT", help="Optional Pin description.")
    return parser.parse_args()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    args = parse_args()

    # Idempotent: does nothing if the tables already exist.
    init_db()

    db = SessionLocal()
    try:
        account = (
            db.query(Account)
            .filter(Account.platform == "pinterest", Account.name == args.account)
            .one_or_none()
        )
        if account is None:
            raise SystemExit(
                f"No pinterest Account named {args.account!r}. "
                'Create it first via the dashboard\'s "Connect Pinterest" button or scripts/add_account.py.'
            )

        payload = {
            "title": args.title or f"Distribution engine test Pin {_timestamp()}",
            "board_id": args.board_id,
        }
        if args.description:
            payload["description"] = args.description

        if args.mode == "image":
            print(f"Uploading {args.file} to R2...")
            staged = upload_file(args.file)
            print(f"Staged at {staged['public_url']}")
            payload["media_public_url"] = staged["public_url"]
        else:
            payload["media_paths"] = [args.file]

        job = Job(
            platform="pinterest",
            payload=payload,
            account_id=account.id,
            status=JobStatus.QUEUED,
        )
        db.add(job)
        db.commit()

        publish_job.delay(job.id)
        print(f"Created job #{job.id} (mode={args.mode}, account={args.account}, board_id={args.board_id}) and dispatched it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
