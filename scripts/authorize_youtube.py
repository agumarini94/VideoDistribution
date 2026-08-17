"""
One-time interactive OAuth2 authorization for the YouTube publisher.

Opens a browser to let the account owner grant upload access, then saves the
resulting credentials (including a refresh token, since we request offline
access) to token.json at the project root. Run it again whenever token.json
is deleted or access is revoked.

Run it from the project root with:
    python -m scripts.authorize_youtube

Requires client_secret.json (an OAuth 2.0 "Desktop app" Client ID downloaded
from Google Cloud Console) to already be in the project root.
"""

from google_auth_oauthlib.flow import InstalledAppFlow

from app.publishers.youtube import CLIENT_SECRET_PATH, SCOPES, TOKEN_PATH


def main() -> None:
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

    TOKEN_PATH.write_text(creds.to_json())
    print(f"Authorization complete. Saved credentials to {TOKEN_PATH}")


if __name__ == "__main__":
    main()
