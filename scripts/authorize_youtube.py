"""
One-time interactive OAuth2 authorization for the YouTube publisher.

Opens a browser to let the account owner grant upload access, then saves the
resulting credentials (including a refresh token, since we request offline
access).

Without --account (single-account mode, unchanged since Phase 2b): saves to
token.json at the project root. Run it again whenever token.json is deleted
or access is revoked.

With --account NAME (Phase 7 — multi-account): saves the credentials JSON
onto an Account row instead (platform="youtube", name=NAME), via the same
insert-or-update logic as scripts/add_account.py — so re-running it with the
same --account NAME rotates that account's stored token in place. Use this
for any YouTube channel beyond the first one.

Run it from the project root with:
    python -m scripts.authorize_youtube
    python -m scripts.authorize_youtube --account "Client X channel"

Requires client_secret.json (an OAuth 2.0 "Desktop app" Client ID downloaded
from Google Cloud Console) to already be in the project root.
"""

import argparse
import json

from google_auth_oauthlib.flow import InstalledAppFlow

from app.db import SessionLocal, init_db
from app.publishers.youtube import CLIENT_SECRET_PATH, SCOPES, TOKEN_PATH
from scripts.add_account import upsert_account


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--account",
        metavar="NAME",
        help='Save credentials to an Account row (platform="youtube", this name) instead of token.json.',
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not CLIENT_SECRET_PATH.exists():
        print(
            "Missing client_secret.json.\n\n"
            "To fix this:\n"
            "  1. In Google Cloud Console, enable the YouTube Data API v3 "
            "for your project.\n"
            "  2. Create an OAuth 2.0 Client ID of type \"Desktop app\".\n"
            "  3. Download it and save it as:\n"
            f"       {CLIENT_SECRET_PATH}\n"
            "  4. Re-run this script: python -m scripts.authorize_youtube"
        )
        return

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)

    # access_type=offline gets us a refresh token instead of just a
    # short-lived access token; prompt=consent forces Google to re-issue one
    # even if this account already authorized the app before.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if args.account:
        init_db()
        db = SessionLocal()
        try:
            account, action = upsert_account(db, "youtube", args.account, json.loads(creds.to_json()))
            db.commit()
            print(f"{action} account #{account.id} (youtube/{args.account}).")
        finally:
            db.close()
    else:
        TOKEN_PATH.write_text(creds.to_json())
        print(f"Authorization complete. Saved credentials to {TOKEN_PATH}")


if __name__ == "__main__":
    main()
