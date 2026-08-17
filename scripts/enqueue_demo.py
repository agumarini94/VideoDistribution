"""
Demo script: creates 10 test jobs and enqueues them for publication.

Run it from the project root with:
    python -m scripts.enqueue_demo

Design decision: this script lives outside app/ because it isn't part of
the engine itself, it's an operations/demo tool. It uses the same building
blocks (Job, SessionLocal, publish_job) that any other real entry point
(an HTTP API, for example) would use, to exercise the end-to-end flow.
"""

from app.db import SessionLocal, init_db
from app.models import Job, JobStatus
from app.tasks import publish_job

PLATFORMS = ["twitter", "instagram", "linkedin", "facebook"]


def build_demo_payload(index: int) -> dict:
    return {
        "text": f"Demo post #{index}",
        "hashtags": ["demo", "distribution-engine"],
    }


def main() -> None:
    # Idempotent: does nothing if the tables already exist.
    init_db()

    db = SessionLocal()
    try:
        jobs = []
        for i in range(10):
            job = Job(
                platform=PLATFORMS[i % len(PLATFORMS)],
                payload=build_demo_payload(i),
                status=JobStatus.QUEUED,
            )
            db.add(job)
            jobs.append(job)

        db.commit()

        # We only have the ids (autoincrement) after the commit, so we
        # enqueue afterwards.
        for job in jobs:
            publish_job.delay(job.id)

        print(f"Created and enqueued {len(jobs)} demo jobs.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
