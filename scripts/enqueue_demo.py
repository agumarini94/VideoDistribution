"""
Demo script: creates 10 test jobs and enqueues them for publication.

Run it from the project root with:
    python -m scripts.enqueue_demo                    # immediate, normal queue (default)
    python -m scripts.enqueue_demo --schedule          # jobs go to their platform's next time slot
    python -m scripts.enqueue_demo --urgent 3          # first 3 jobs dispatch immediately via the priority queue
    python -m scripts.enqueue_demo --schedule --urgent 3   # 3 urgent now, the other 7 scheduled

Design decision: this script lives outside app/ because it isn't part of
the engine itself, it's an operations/demo tool. It uses the same building
blocks (Job, SessionLocal, publish_job) that any other real entry point
(an HTTP API, for example) would use, to exercise the end-to-end flow.
--schedule/--urgent branching lives here rather than behind a new app/
abstraction because this script is still the only job-creation call site;
extracting a helper before a second caller exists would be premature.
"""

import argparse
from datetime import datetime, timezone

from app.celery_app import PRIORITY_QUEUE_NAME
from app.config import next_slot_for
from app.db import SessionLocal, init_db
from app.models import Job, JobStatus
from app.tasks import publish_job

PLATFORMS = ["twitter", "instagram", "linkedin", "facebook"]


def build_demo_payload(index: int) -> dict:
    return {
        "text": f"Demo post #{index}",
        "hashtags": ["demo", "distribution-engine"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Create jobs as SCHEDULED, at their platform's next time slot, instead of dispatching immediately.",
    )
    parser.add_argument(
        "--urgent",
        type=int,
        default=0,
        metavar="N",
        help="Mark the first N jobs urgent: dispatched immediately via the priority queue, bypassing --schedule.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Idempotent: does nothing if the tables already exist.
    init_db()

    # Aware UTC: next_slot_for (app/config.py, Phase 20) treats a naive
    # `now` as already-UTC, so passing local naive time here would silently
    # shift every computed slot by the machine's UTC offset.
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        jobs = []
        for i in range(10):
            platform = PLATFORMS[i % len(PLATFORMS)]
            is_urgent = i < args.urgent

            if is_urgent:
                # Urgent skips scheduling entirely, even if --schedule was also passed.
                job = Job(platform=platform, payload=build_demo_payload(i), status=JobStatus.QUEUED)
            elif args.schedule:
                job = Job(
                    platform=platform,
                    payload=build_demo_payload(i),
                    status=JobStatus.SCHEDULED,
                    scheduled_at=next_slot_for(platform, now),
                )
            else:
                job = Job(platform=platform, payload=build_demo_payload(i), status=JobStatus.QUEUED)

            db.add(job)
            jobs.append((job, is_urgent))

        db.commit()

        # We only have the ids (autoincrement) after the commit, so we
        # dispatch afterwards. SCHEDULED jobs aren't dispatched here at
        # all — dispatch_due_jobs (Celery Beat) picks them up once due.
        dispatched, scheduled = 0, 0
        for job, is_urgent in jobs:
            if is_urgent:
                publish_job.apply_async(args=[job.id], queue=PRIORITY_QUEUE_NAME)
                dispatched += 1
            elif job.status == JobStatus.SCHEDULED:
                scheduled += 1
            else:
                publish_job.delay(job.id)
                dispatched += 1

        print(
            f"Created {len(jobs)} demo jobs: {dispatched} dispatched now "
            f"({args.urgent} via the priority queue), {scheduled} scheduled "
            "for their next time slot (picked up by dispatch_due_jobs)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
