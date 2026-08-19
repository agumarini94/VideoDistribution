"""
Prints a small table of jobs (id, platform, account, status, attempts,
scheduled_at) so scheduling, priority dispatch and per-account routing can
be observed without opening Neon directly.

Run it from the project root with:
    python -m scripts.show_jobs
"""

from app.db import SessionLocal
from app.models import Account, Job

_COLUMNS = ("id", "platform", "account", "status", "attempts", "scheduled_at")


def main() -> None:
    db = SessionLocal()
    try:
        # Outer join: most jobs have no account_id yet (Phase 6 is
        # additive), and those should still show up with "-" instead of
        # being dropped by an inner join.
        rows_raw = (
            db.query(Job, Account.name)
            .outerjoin(Account, Job.account_id == Account.id)
            .order_by(Job.id)
            .all()
        )
    finally:
        db.close()

    if not rows_raw:
        print("No jobs found.")
        return

    rows = [
        (
            str(job.id),
            job.platform,
            account_name or "-",
            job.status.value,
            str(job.attempts),
            job.scheduled_at.isoformat(timespec="seconds") if job.scheduled_at else "-",
        )
        for job, account_name in rows_raw
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
