"""
Creates one real threads job (text, image, or video post) and dispatches
it — smoke-tests app/publishers/threads.py end-to-end (Phase 29f) the same
way scripts/enqueue_instagram_test.py does for Instagram.

Unlike scripts/enqueue_instagram_test.py (which always needs media), Threads
has a real text-only post type, so --mode text needs no --file and no R2
upload at all. For --mode image/video, this script uploads the given file
to Cloudflare R2 itself before creating the job (same reason
enqueue_instagram_test.py does): app/publishers/threads.py never reads a
local file for media (Meta downloads it from a public URL at publish time,
for images AND videos — unlike Pinterest's video Pins), so the job payload
needs media_public_url, not a local media_paths entry. This will fail
loudly if R2 isn't configured (R2_ENDPOINT_URL/R2_ACCESS_KEY_ID/
R2_SECRET_ACCESS_KEY/R2_BUCKET_NAME/R2_PUBLIC_BASE_URL in .env).

Like Pinterest, Threads has no single-account/env-var fallback: --account is
always required, and the Account must already exist — connect one via the
dashboard's self-service "Connect Threads" button (client_user session), or
scripts/add_account.py by hand.

Run it from the project root with:
    python -m scripts.enqueue_threads_test --mode text --text "Hello from the API" --account "Main account"
    python -m scripts.enqueue_threads_test --mode image --file photo.jpg --account "Main account"
    python -m scripts.enqueue_threads_test --mode video --file clip.mp4 --account "Main account"
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
        choices=["text", "image", "video"],
        help="What kind of post this is. --mode text needs no --file; image/video require one and are staged "
        "to R2 (app/publishers/threads.py auto-detects image vs. video from the staged URL's guessed MIME type).",
    )
    parser.add_argument("--file", metavar="PATH", help="Local media file (required for --mode image/video).")
    parser.add_argument(
        "--account",
        required=True,
        metavar="NAME",
        help='Link the job to an existing Account row (platform="threads", this name). Required — no single-account fallback.',
    )
    parser.add_argument("--text", metavar="TEXT", help="Post text (<=500 chars). Auto-generated if omitted for image/video.")
    return parser.parse_args()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    args = parse_args()

    if args.mode in ("image", "video") and not args.file:
        raise SystemExit(f"--mode {args.mode} requires --file")
    if args.mode == "text" and not args.text:
        raise SystemExit("--mode text requires --text")

    # Idempotent: does nothing if the tables already exist.
    init_db()

    db = SessionLocal()
    try:
        account = (
            db.query(Account)
            .filter(Account.platform == "threads", Account.name == args.account)
            .one_or_none()
        )
        if account is None:
            raise SystemExit(
                f"No threads Account named {args.account!r}. "
                'Create it first via the dashboard\'s "Connect Threads" button or scripts/add_account.py.'
            )

        payload = {}
        if args.mode == "text":
            payload["text"] = args.text
        else:
            print(f"Uploading {args.file} to R2...")
            staged = upload_file(args.file)
            print(f"Staged at {staged['public_url']}")
            payload["media_public_url"] = staged["public_url"]
            payload["text"] = args.text or f"Distribution engine test post ({args.mode}) {_timestamp()}"

        job = Job(
            platform="threads",
            payload=payload,
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
