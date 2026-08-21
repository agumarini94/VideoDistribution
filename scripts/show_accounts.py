"""
Prints a small table of accounts (id, platform, name, active, token_expiry,
updated_at), companion to scripts/show_jobs.py — lets token freshness be
observed (Phase 8) without opening Neon directly.

token_expiry is parsed straight from Account.credentials["expiry"] when
present (the ISO 8601 string Google's Credentials.to_json() produces, see
app/publishers/youtube.py). Accounts on platforms without expiring tokens
(e.g. twitter's OAuth 1.0a) or without a stored expiry show "-".

Run it from the project root with:
    python -m scripts.show_accounts
"""

from datetime import datetime

from app.db import SessionLocal
from app.models import Account

_COLUMNS = ("id", "platform", "name", "active", "token_expiry", "updated_at")


def _format_expiry(credentials: dict) -> str:
    expiry_raw = credentials.get("expiry") if isinstance(credentials, dict) else None
    if not expiry_raw:
        return "-"
    try:
        return datetime.fromisoformat(expiry_raw).isoformat(timespec="seconds")
    except ValueError:
        return f"invalid ({expiry_raw})"


def main() -> None:
    db = SessionLocal()
    try:
        accounts = db.query(Account).order_by(Account.id).all()
    finally:
        db.close()

    if not accounts:
        print("No accounts found.")
        return

    rows = [
        (
            str(account.id),
            account.platform,
            account.name,
            str(account.is_active),
            _format_expiry(account.credentials),
            account.updated_at.isoformat(timespec="seconds") if account.updated_at else "-",
        )
        for account in accounts
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
