"""
One-time interactive OAuth2 authorization for the TikTok publisher
(Sandbox mode — see app/publishers/tiktok.py and CLAUDE.md Phase 10).

Opens a browser to let the account owner grant access (video.upload +
user.info.basic), waits for the authorization code, exchanges it for an
access/refresh token pair, and saves the resulting credentials onto an
Account row (platform="tiktok", --account NAME), via the same
insert-or-update logic as scripts/add_account.py — so re-running it with the
same --account NAME rotates that account's stored token in place.

TIKTOK_REDIRECT_URI vs. the local callback server: the TikTok Developer
Portal rejects localhost/127.0.0.1 redirect URIs outright, so
TIKTOK_REDIRECT_URI is a public HTTPS page (see .env.example / README /
CLAUDE.md for the GitHub Pages forwarder trick) that immediately forwards
the callback's query string to http://localhost:<TIKTOK_LOCAL_CALLBACK_PORT>/callback
via JS (location.replace) — that's what this script's local one-shot HTTP
server actually listens on. TIKTOK_REDIRECT_URI itself is never touched by
this process; it's only ever sent to TikTok (authorize URL + token
exchange), which is why it has to match the portal exactly on both calls.

Unlike YouTube/Twitter, TikTok has no single-account fallback in this
project (see app/publishers/tiktok.py) — --account is required.

Sandbox limitation: this only works for target accounts registered as
testers in the TikTok Developer Portal for this app, until app review
passes and video.publish is granted.

Run it from the project root with:
    python -m scripts.authorize_tiktok --account "Client X account"

Requires TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI to
already be set in .env (the public forwarder page registered in the
Developer Portal). TIKTOK_LOCAL_CALLBACK_PORT is optional (default 8910) —
it only needs to change if that port is already taken locally, and the
forwarder page must then be updated to match.
"""

import argparse
import hashlib
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from app.db import SessionLocal, init_db
from app.publishers.tiktok import AUTHORIZE_URL, SCOPES, exchange_authorization_code
from scripts.add_account import upsert_account


class _CallbackHandler(BaseHTTPRequestHandler):
    """
    Handles exactly one GET request (the OAuth redirect) and stores its
    query params on the class so the caller can read them after
    handle_request() returns.
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


def _generate_pkce_pair() -> tuple[str, str]:
    """
    TikTok's OAuth requires PKCE (RFC 7636): the authorize request carries a
    code_challenge, and the token exchange later proves it holds the
    matching code_verifier. token_urlsafe's alphabet (A-Za-z0-9-_) is a
    subset of the "unreserved" charset PKCE requires, so no extra
    validation/escaping is needed.

    DELIBERATE RFC 7636 DEVIATION: standard PKCE encodes the challenge as
    BASE64URL(SHA256(verifier)) with no padding. TikTok's Login Kit for
    Desktop does NOT follow that — it expects the raw HEX digest of
    SHA256(verifier) instead (confirmed against
    developers.tiktok.com/doc/login-kit-desktop; the base64url form gets
    rejected with a "code_challenge" error on their login page). Do not
    "fix" this back to base64url — it's non-standard on TikTok's end, not a
    bug here.
    """
    verifier = secrets.token_urlsafe(64)  # ~86 chars, within RFC 7636's 43-128 range
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    return verifier, challenge


def _wait_for_callback(port: int) -> dict:
    """
    Listens on localhost:port for exactly one GET /callback — the request
    the public forwarder page (TIKTOK_REDIRECT_URI) issues via
    location.replace() once TikTok redirects the browser to it. Always
    localhost: the forwarder page is what's actually registered with
    TikTok, this server is purely a local relay target for it.
    """
    server = HTTPServer(("localhost", port), _CallbackHandler)
    try:
        server.handle_request()  # blocks for exactly one request, then returns
    finally:
        server.server_close()
    return _CallbackHandler.result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--account",
        required=True,
        metavar="NAME",
        help='Save credentials to an Account row (platform="tiktok", this name).',
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    client_key = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("TIKTOK_REDIRECT_URI", "").strip()
    local_port = int(os.getenv("TIKTOK_LOCAL_CALLBACK_PORT", "8910").strip())
    missing = [
        name
        for name, value in (
            ("TIKTOK_CLIENT_KEY", client_key),
            ("TIKTOK_CLIENT_SECRET", client_secret),
            ("TIKTOK_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        print(
            "Missing TikTok app credentials.\n\n"
            "To fix this:\n"
            "  1. In the TikTok for Developers portal, create/select your app "
            "and add this account as a Sandbox target user.\n"
            "  2. Under Login Kit, register a redirect URI. The portal "
            "rejects localhost/127.0.0.1, so this must be a public HTTPS "
            "page (e.g. a GitHub Pages callback.html) that forwards the "
            "callback's query string to "
            "http://localhost:8910/callback via JS location.replace() — "
            "see CLAUDE.md Phase 10 for the exact trick. Request the "
            "video.upload scope while you're there (Sandbox mode grants it "
            "without review; video.publish needs app review).\n"
            "  3. Set in .env: " + ", ".join(missing) + " (TIKTOK_REDIRECT_URI "
            "is the public forwarder page's URL, not the localhost one)\n"
            "  4. Re-run: python -m scripts.authorize_tiktok --account NAME"
        )
        return

    state = secrets.token_urlsafe(16)
    code_verifier, code_challenge = _generate_pkce_pair()
    query = urlencode(
        {
            "client_key": client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    print(f"Opening browser for authorization:\n  {AUTHORIZE_URL}?{query}\n")
    webbrowser.open(f"{AUTHORIZE_URL}?{query}")

    print(f"Waiting for the forwarder page ({redirect_uri}) to relay the callback to http://localhost:{local_port}/callback ...")
    callback = _wait_for_callback(local_port)

    if callback.get("state") != state:
        print("State mismatch on the OAuth callback (possible CSRF, or a stale browser tab) — aborting. Re-run the script.")
        return

    code = callback.get("code")
    if not code:
        error = callback.get("error_description") or callback.get("error") or "no code received"
        print(f"Authorization failed: {error}")
        return

    credentials = exchange_authorization_code(code, redirect_uri, code_verifier)

    init_db()
    db = SessionLocal()
    try:
        account, action = upsert_account(db, "tiktok", args.account, credentials)
        db.commit()
        print(f"{action} account #{account.id} (tiktok/{args.account}).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
