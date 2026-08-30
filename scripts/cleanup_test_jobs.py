"""
Deletes jobs whose platform is not one of the real, wired-up publishers
("youtube", "tiktok", "twitter") — leftovers from the fake-publisher demo
era (instagram/facebook/linkedin/etc., see scripts/enqueue_demo.py), along
with any webhook_events rows that reference them.

Defaults to a dry run: prints what would be deleted, with counts per
platform, and makes no database changes. Pass --execute to actually delete.

Run it from the project root with:
    python -m scripts.cleanup_test_jobs
    python -m scripts.cleanup_test_jobs --execute
"""

import argparse
from collections import Counter

from app.db import SessionLocal
from app.models import Job, WebhookEvent

_REAL_PLATFORMS = ("youtube", "tiktok", "twitter")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete the jobs (and their webhook_events rows). Default is a dry run.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    db = SessionLocal()
    try:
        jobs = db.query(Job).filter(~Job.platform.in_(_REAL_PLATFORMS)).order_by(Job.id).all()

        if not jobs:
            print(f"No jobs found outside {_REAL_PLATFORMS!r}. Nothing to do.")
            return

        counts = Counter(job.platform for job in jobs)
        job_ids = [job.id for job in jobs]

        external_ids = [job.external_id for job in jobs if job.external_id is not None]
        webhook_events = (
            db.query(WebhookEvent).filter(WebhookEvent.publish_id.in_(external_ids)).all()
            if external_ids
            else []
        )

        verb = "Deleting" if args.execute else "Would delete"
        print(f"{verb} {len(jobs)} job(s):")
        for platform, count in sorted(counts.items()):
            print(f"  {platform}: {count}")
        print(f"{verb} {len(webhook_events)} related webhook_events row(s).")

        if not args.execute:
            print("\nDry run only — pass --execute to actually delete.")
            return

        for event in webhook_events:
            db.delete(event)
        db.query(Job).filter(Job.id.in_(job_ids)).delete(synchronize_session=False)
        db.commit()

        print("\nDone.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
