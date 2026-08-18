"""
Prints a small table of jobs (id, platform, status, attempts, scheduled_at)
so scheduling and priority dispatch can be observed without opening Neon
directly.

Run it from the project root with:
    python -m scripts.show_jobs
"""

from app.db import SessionLocal
from app.models import Job

_COLUMNS = ("id", "platform", "status", "attempts", "scheduled_at")


def main() -> None:
    db = SessionLocal()
    try:
        jobs = db.query(Job).order_by(Job.id).all()
    finally:
        db.close()

    if not jobs:
        print("No jobs found.")
        return

    rows = [
        (
            str(job.id),
            job.platform,
            job.status.value,
            str(job.attempts),
            job.scheduled_at.isoformat(timespec="seconds") if job.scheduled_at else "-",
        )
        for job in jobs
    ]

    widths = [max(len(col), *(len(row[i]) for row in rows)) for i, col in enumerate(_COLUMNS)]

    def format_row(row: tuple) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    print(format_row(tuple(col.upper() for col in _COLUMNS)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(format_row(row))


if __name__ == "__main__":
    main()
