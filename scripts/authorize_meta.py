"""
One-time interactive OAuth2 authorization for the Meta (Facebook + Instagram)
publisher foundation (Phase 23 — see app/publishers/meta.py and CLAUDE.md).

Runs the full Graph API OAuth chain documented in app/publishers/meta.py:
  1. Opens a browser to the Facebook Login dialog.
  2. Waits for the redirect carrying ?code=...&state=....
  3. Exchanges the code for a short-lived user token, then upgrades that to
     a long-lived (~60 day) user token.
  4. Lists the Pages the user manages (GET /me/accounts) and their Page
     tokens.
  5. If more than one Page comes back, prompts the operator to choose one.
  6. Looks up the chosen Page's linked Instagram Business account.

Saves the result onto Account row(s) via the same upsert_account helper as
every other authorize script in this project:
  - platform "facebook": {page_id, page_token, page_name, user_token,
    user_token_expires_at}
  - platform "instagram" (only if the chosen Page has a linked Instagram
    Business account): {ig_user_id, page_id, page_token, user_token,
    user_token_expires_at}

If the Page has no linked Instagram Business account, only the facebook
Account is created, and a warning is printed explaining the Business-account
+ Page-link requirement (see _NO_INSTAGRAM_WARNING below).

Account naming: defaults to the Facebook Page's own name, or use --account
NAME to override (e.g. when multiple client Pages happen to share a display
name). Re-running with the same resulting name rotates that Account's
credentials in place, same as every other authorize_*.py script.

Redirect URI / localhost caveat — UNVERIFIED, flag before relying on it:
unlike TikTok's Developer Portal (which rejects localhost/127.0.0.1 redirect
URIs outright, see scripts/authorize_tiktok.py), Meta's App Dashboard is
documented to allow http://localhost redirect URIs for an app still in
Development mode. This script assumes that and binds its one-shot callback
server directly to META_REDIRECT_URI's host:port — no public forwarder page
needed. If the real App Dashboard rejects a localhost URI once META_APP_ID
exists, this needs the same public-forwarder-page trick as
scripts/authorize_tiktok.py (see CLAUDE.md Phase 10) — treat that as a
follow-up, not a sign this script is broken.

Run it from the project root with:
    python -m scripts.authorize_meta [--account NAME]

Requires META_APP_ID, META_APP_SECRET and META_REDIRECT_URI to already be
set in .env.
"""

import argparse
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from app.db import SessionLocal, init_db
from app.publishers.meta import (
    AUTHORIZE_URL,
    SCOPES,
    exchange_code_for_user_token,
    exchange_long_lived_token,
    get_instagram_business_account,
    list_pages,
)
from scripts.add_account import upsert_account

_NO_INSTAGRAM_WARNING = (
    "WARNING: Page {page_name!r} has no linked Instagram Business account "
    "(instagram_business_account was empty in the Graph API response). Only "
    "the facebook Account was created.\n"
    "To enable Instagram publishing for this Page:\n"
    "  1. Make sure the target Instagram account is a Business (or Creator) "
    "account, not a Personal one.\n"
    "  2. Link it to this Facebook Page (from the Instagram app: Settings > "
    "Account > Linked Accounts > Facebook; or from the Facebook Page: "
    "Settings > Linked Accounts).\n"
    "  3. Re-run: python -m scripts.authorize_meta"
)


class _CallbackHandler(BaseHTTPRequestHandler):
    """
    Handles exactly one GET request (the OAuth redirect) and stores its
    query params on the class so the caller can read them after
    handle_request() returns. Same pattern as scripts/authorize_tiktok.py.
    """

    result: dict = {}

    def do_GET(self) -> None:
        _CallbackHandler.result = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body>Authorization complete, you can close this tab and return to the terminal.</body></html>")

    def log_message(self, format: str, *args) -> None:  # silence default request logging
        pass


def _wait_for_callback(host: str, port: int) -> dict:
    server = HTTPServer((host, port), _CallbackHandler)
    try:
        server.handle_request()  # blocks for exactly one request, then returns
    finally:
        server.server_close()
    return _CallbackHandler.result


def _choose_page(pages: list[dict]) -> dict:
    """
    Returns the only Page directly if there's just one; otherwise prints a
    numbered list and prompts the operator to pick one via input().
    """
    if len(pages) == 1:
        return pages[0]
    print(f"\nThis user manages {len(pages)} Pages:")
    for index, page in enumerate(pages):
        print(f"  [{index}] {page.get('name')} (id={page.get('id')})")
    while True:
        choice = input(f"Pick a Page [0-{len(pages) - 1}]: ").strip()
        if choice.isdigit() and 0 <= int(choice) < len(pages):
            return pages[int(choice)]
        print("Invalid choice, try again.")


def _run_authorization(code: str, redirect_uri: str, account_name: str | None) -> None:
    """
    Everything after a valid authorization code has been received: the
    token exchange chain, Page/Instagram discovery, and persisting the
    resulting Account row(s). Split out from main() so it can be exercised
    directly in tests with exchange_code_for_user_token/
    exchange_long_lived_token/list_pages/get_instagram_business_account
    monkeypatched — no real browser or local HTTP server needed.
    """
    short_lived = exchange_code_for_user_token(code, redirect_uri)
    long_lived = exchange_long_lived_token(short_lived["access_token"])
    user_token = long_lived["access_token"]
    user_token_expires_at = long_lived["expires_at"]

    pages = list_pages(user_token)
    if not pages:
        print("This user manages no Facebook Pages — nothing to authorize. Grant Page access and re-run.")
        return

    page = _choose_page(pages)
    page_id = page["id"]
    page_token = page["access_token"]
    page_name = page.get("name", "")
    name = account_name or page_name

    ig_user_id = get_instagram_business_account(page_id, page_token)

    init_db()
    db = SessionLocal()
    try:
        facebook_credentials = {
            "page_id": page_id,
            "page_token": page_token,
            "page_name": page_name,
            "user_token": user_token,
            "user_token_expires_at": user_token_expires_at,
        }
        account, action = upsert_account(db, "facebook", name, facebook_credentials)
        db.commit()
        print(f"{action} account #{account.id} (facebook/{name}).")

        if ig_user_id:
            instagram_credentials = {
                "ig_user_id": ig_user_id,
                "page_id": page_id,
                "page_token": page_token,
                "user_token": user_token,
                "user_token_expires_at": user_token_expires_at,
            }
            ig_account, ig_action = upsert_account(db, "instagram", name, instagram_credentials)
            db.commit()
            print(f"{ig_action} account #{ig_account.id} (instagram/{name}).")
        else:
            print("\n" + _NO_INSTAGRAM_WARNING.format(page_name=page_name))
    finally:
        db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--account",
        metavar="NAME",
        help="Override the Account name (default: the Facebook Page's own name).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    app_id = os.getenv("META_APP_ID", "").strip()
    app_secret = os.getenv("META_APP_SECRET", "").strip()
    redirect_uri = os.getenv("META_REDIRECT_URI", "").strip()
    missing = [
        name
        for name, value in (("META_APP_ID", app_id), ("META_APP_SECRET", app_secret), ("META_REDIRECT_URI", redirect_uri))
        if not value
    ]
    if missing:
        print(
            "Missing Meta app credentials.\n\n"
            "To fix this:\n"
            "  1. Create an app at developers.facebook.com/apps (or use an "
            "existing one), add the Facebook Login product, and register a "
            "redirect URI matching META_REDIRECT_URI. A localhost URI is "
            "assumed to work for a Development-mode app — if the dashboard "
            "rejects it, see this script's module docstring for the "
            "fallback (same public-forwarder-page trick as "
            "scripts/authorize_tiktok.py).\n"
            "  2. Request the pages_manage_posts, pages_show_list, "
            "pages_read_engagement, instagram_basic, "
            "instagram_content_publish, business_management permissions "
            "(most need App Review before they work for anyone other than "
            "the app's own admins/developers/testers).\n"
            "  3. Set in .env: " + ", ".join(missing) + "\n"
            "  4. Re-run: python -m scripts.authorize_meta"
        )
        return

    parsed_redirect = urlparse(redirect_uri)
    host = parsed_redirect.hostname or "localhost"
    port = parsed_redirect.port or 80

    state = secrets.token_urlsafe(16)
    query = urlencode({"client_id": app_id, "redirect_uri": redirect_uri, "state": state, "scope": SCOPES})
    print(f"Opening browser for authorization:\n  {AUTHORIZE_URL}?{query}\n")
    webbrowser.open(f"{AUTHORIZE_URL}?{query}")

    print(f"Waiting for the OAuth callback on {host}:{port} ...")
    callback = _wait_for_callback(host, port)

    if callback.get("state") != state:
        print("State mismatch on the OAuth callback (possible CSRF, or a stale browser tab) — aborting. Re-run the script.")
        return

    code = callback.get("code")
    if not code:
        error = callback.get("error_description") or callback.get("error") or "no code received"
        print(f"Authorization failed: {error}")
        return

    _run_authorization(code, redirect_uri, args.account)


if __name__ == "__main__":
    main()
