"""
CLI to insert or update an Account row (per-account credentials for a
platform's publisher — see app/models.py Account and CLAUDE.md Phase 6).

Run it from the project root with:
    python -m scripts.add_account --platform twitter --name "Main account" \\
        access_token=xxx access_token_secret=yyy

Credentials are given as any number of key=value pairs; which keys are
expected depends on the platform's publisher (e.g. app/publishers/twitter.py
expects access_token / access_token_secret).

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
        account = (
            db.query(Account)
            .filter(Account.platform == args.platform, Account.name == args.name)
            .one_or_none()
        )
        if account is None:
            account = Account(platform=args.platform, name=args.name, credentials=credentials)
            db.add(account)
            action = "Created"
        else:
            account.credentials = credentials
            action = "Updated"

        account.is_active = not args.inactive
        db.commit()

        print(f"{action} account #{account.id} ({args.platform}/{args.name}), active={account.is_active}.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
