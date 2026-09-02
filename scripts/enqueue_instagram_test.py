"""
Creates one real instagram job (image or Reels video post) and dispatches
it — smoke-tests app/publishers/instagram.py end-to-end (Phase 25) the same
way scripts/enqueue_facebook_test.py does for Facebook.

Unlike every other enqueue_*_test.py script, this one uploads the given
file to Cloudflare R2 itself before creating the job: app/publishers/instagram.py
never reads a local file (Meta downloads media from a public URL at publish
time), so the job payload needs media_public_url, not a local media_paths
entry — see app/storage.py and CLAUDE.md Phase 22/25. This will fail loudly
if R2 isn't configured (R2_ENDPOINT_URL/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/
R2_BUCKET_NAME/R2_PUBLIC_BASE_URL in .env).

Like Facebook, Instagram has no single-account/env-var fallback (Phase 23's
decision): --account is always required, and the Account must already exist
(create it with scripts/authorize_meta.py, which links a Page's Instagram
Business account and stores its ig_user_id/page_token).

Run it from the project root with:
    python -m scripts.enqueue_instagram_test --mode image --file photo.jpg --account "Main Page"
    python -m scripts.enqueue_instagram_test --mode video --file clip.mp4 --account "Main Page"
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
        help="What kind of post this is (informational — app/publishers/instagram.py auto-detects the "
        "actual media type from the uploaded file's guessed MIME type).",
    )
    parser.add_argument("--file", required=True, metavar="PATH", help="Local media file to upload to R2 and publish.")
    parser.add_argument(
        "--account",
        required=True,
        metavar="NAME",
        help='Link the job to an existing Account row (platform="instagram", this name). Required — no single-account fallback.',
    )
    parser.add_argument("--caption", metavar="TEXT", help="Optional caption. Auto-generated if omitted.")
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
            .filter(Account.platform == "instagram", Account.name == args.account)
            .one_or_none()
        )
        if account is None:
            raise SystemExit(
                f"No instagram Account named {args.account!r}. "
                "Create it first with scripts/authorize_meta.py."
            )

        print(f"Uploading {args.file} to R2...")
        staged = upload_file(args.file)
        print(f"Staged at {staged['public_url']}")

        payload = {
            "text": args.caption or f"Distribution engine test post ({args.mode}) {_timestamp()}",
            "media_public_url": staged["public_url"],
        }

        job = Job(
            platform="instagram",
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
