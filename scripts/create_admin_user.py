"""
Bootstraps an admin User row (Phase 28 — see app/models.py::User and
CLAUDE.md). This is the ONLY way to create a role="admin" row: self-
registration (POST /api/auth/register) always creates role="client_user",
is_approved=False, and the admin-approval flow only ever assigns/confirms a
Client on an existing pending client_user request — neither path can ever
produce an admin.

Run it from the project root with:
    python -m scripts.create_admin_user --email admin@example.com

Prompts for the password interactively (getpass, twice) rather than taking
it as a CLI argument, so it never ends up in shell history or process
listings. Re-running with the same email resets that admin's password (and
forces role="admin"/client_id=None/is_approved=True) in place, the same
upsert-by-identity spirit as scripts/add_account.py.

Note: DASHBOARD_USERNAME/DASHBOARD_PASSWORD (HTTP Basic Auth) is a
completely separate, unchanged admin mechanism — this script has nothing to
do with it. A User row created here logs in through the dashboard's own
Login screen (POST /api/auth/login), which the Basic-Auth env vars don't.
"""

import argparse
import getpass

from app.auth import hash_password
from app.db import SessionLocal, init_db
from app.models import User


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="Login email for the new/existing admin.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    email = args.email.strip().lower()
    if not email or "@" not in email:
        raise SystemExit(f"Invalid email: {args.email!r}")

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("Passwords do not match.")
    if len(password) < 8:
        raise SystemExit("Password must be at least 8 characters.")

    # Idempotent: does nothing if the tables already exist. The `users`
    # table itself is brand new (Phase 28) but needs no manual Neon
    # migration step the way Phase 26's client_id columns did — create_all
    # creates whole new tables automatically, it just can't ALTER existing
    # ones. See CLAUDE.md Phase 28.
    init_db()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one_or_none()
        if user is None:
            user = User(
                email=email,
                hashed_password=hash_password(password),
                role="admin",
                client_id=None,
                is_approved=True,
            )
            db.add(user)
            action = "Created"
        else:
            user.hashed_password = hash_password(password)
            user.role = "admin"
            user.client_id = None
            user.is_approved = True
            action = "Updated"

        db.commit()
        db.refresh(user)
        print(f"{action} admin user #{user.id} ({user.email}). They can now log in via the dashboard's Login screen.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
