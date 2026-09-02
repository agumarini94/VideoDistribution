"""
CLI to insert or update an Account row (per-account credentials for a
platform's publisher — see app/models.py Account and CLAUDE.md Phase 6).

Run it from the project root with:
    python -m scripts.add_account --platform tiktok --name "Main account" \\
        access_token=xxx refresh_token=yyy

    # Twitter/X (Phase 21, OAuth 2.0 + API v2 — see app/publishers/twitter.py):
    python -m scripts.add_account --platform twitter --name "Main account" \\
        client_id=xxx client_secret=yyy access_token=zzz refresh_token=www \\
        expires_at=2026-09-01T14:00:00+00:00

    # Meta (Phase 23 — see app/publishers/meta.py). Normally created via
    # scripts/authorize_meta.py instead of by hand, since it needs a Page
    # token + (for instagram) a discovered ig_user_id, not just a token pair:
    python -m scripts.add_account --platform facebook --name "Main Page" \\
        page_id=xxx page_token=yyy page_name="Main Page" user_token=zzz \\
        user_token_expires_at=2026-11-01T14:00:00+00:00

Credentials are given as any number of key=value pairs; which keys are
expected depends on the platform's publisher (e.g. app/publishers/twitter.py
expects client_id / client_secret / access_token / refresh_token, plus an
optional expires_at ISO datetime used by the proactive token-refresh Beat
job — see token_expires_within/refresh_stored_credentials there). X's
refresh tokens are single-use and rotate on every refresh, so re-running
this script to hand-set a refresh_token should only ever be needed for the
very first registration or a full re-authorization — normal rotation is
handled automatically by app/tasks.py.

If an Account with the same platform+name already exists, its credentials
(and is_active) are updated in place instead of creating a duplicate — so
re-running this script is how you rotate a token.
"""

import argparse

from app.db import SessionLocal, init_db
from app.models import Account


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", required=True, help='e.g. "twitter"')
    parser.add_argument("--name", required=True, help='Human label, e.g. "Main account"')
    parser.add_argument("--inactive", action="store_true", help="Create/update the account as inactive.")
    parser.add_argument(
        "credentials",
        nargs="+",
        metavar="key=value",
        help="Credential fields as key=value pairs, e.g. access_token=... access_token_secret=...",
    )
    return parser.parse_args()


def parse_credentials(pairs: list[str]) -> dict:
    credentials = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"Invalid credential {pair!r}: expected key=value.")
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"Invalid credential {pair!r}: empty key.")
        credentials[key] = value
    return credentials


def upsert_account(db, platform: str, name: str, credentials: dict, is_active: bool = True) -> tuple[Account, str]:
    """
    Shared insert-or-update logic: matches on platform+name to decide
    whether to create a new Account row or rotate credentials on an
    existing one. Used by this script's CLI and by
    scripts/authorize_youtube.py --account. Returns (account, action) where
    action is "Created" or "Updated", for the caller to report.

    Does not commit — the caller controls the transaction boundary.
    """
    account = db.query(Account).filter(Account.platform == platform, Account.name == name).one_or_none()
    if account is None:
        account = Account(platform=platform, name=name, credentials=credentials)
        db.add(account)
        action = "Created"
    else:
        account.credentials = credentials
        action = "Updated"

    account.is_active = is_active
    return account, action


def main() -> None:
    args = parse_args()
    credentials = parse_credentials(args.credentials)

    # Idempotent: does nothing if the tables already exist. Note: on an
    # existing Neon database, this creates the new `accounts` table but
    # does NOT add jobs.account_id — see the "when credentials arrive"
    # checklist in CLAUDE.md (Phase 6) for the manual ALTER TABLE needed.
    init_db()

    db = SessionLocal()
    try:
        account, action = upsert_account(db, args.platform, args.name, credentials, is_active=not args.inactive)
        db.commit()

        print(f"{action} account #{account.id} ({args.platform}/{args.name}), active={account.is_active}.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
