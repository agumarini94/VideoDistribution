"""
Uploads a small test file to R2, prints its signed URL, then deletes it —
so R2 credentials can be verified independently of any real media flow.

Run it from the project root with:
    python -m scripts.test_storage
"""

import tempfile
from pathlib import Path

from app.exceptions import StorageNotConfiguredError
from app.storage import delete_media, generate_signed_url, upload_media

TEST_KEY = "distribution-engine/test-storage-check.txt"


def main() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("distribution-engine R2 storage test file\n")
        local_path = Path(f.name)

    try:
        upload_media(str(local_path), TEST_KEY)
        print(f"Uploaded test file to key: {TEST_KEY}")

        url = generate_signed_url(TEST_KEY, expires_seconds=300)
        print(f"Signed URL (valid 5 min): {url}")

        delete_media(TEST_KEY)
        print("Deleted test file. R2 storage is working correctly.")
    except StorageNotConfiguredError as exc:
        print(
            f"{exc}\n\n"
            "Env vars needed (see .env.example):\n"
            "  R2_ENDPOINT_URL       - e.g. https://<account_id>.r2.cloudflarestorage.com\n"
            "  R2_ACCESS_KEY_ID      - from an R2 API token\n"
            "  R2_SECRET_ACCESS_KEY  - from the same R2 API token\n"
            "  R2_BUCKET_NAME        - the target bucket's name"
        )
    finally:
        local_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
