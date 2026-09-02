"""
Quick end-to-end check of the R2 upload_file() flow (Phase 22): uploads a
tiny generated text file, prints its key and public_url, then deletes it —
so R2 credentials can be verified from a terminal, independently of the job
pipeline. Complements scripts/test_storage.py (which exercises the lower-
level upload_media/generate_signed_url/delete_media functions directly).

Run it from the project root with:
    python -m scripts.r2_smoke_test
"""

import tempfile
from pathlib import Path

from app.exceptions import StorageNotConfiguredError
from app.storage import delete_media, upload_file


def main() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("distribution-engine R2 smoke test file\n")
        local_path = Path(f.name)

    try:
        result = upload_file(str(local_path))
    except StorageNotConfiguredError as exc:
        print(
            f"{exc}\n\n"
            "Env vars needed (see .env.example):\n"
            "  R2_ENDPOINT_URL       - e.g. https://<account_id>.r2.cloudflarestorage.com\n"
            "  R2_ACCESS_KEY_ID      - from an R2 API token\n"
            "  R2_SECRET_ACCESS_KEY  - from the same R2 API token\n"
            "  R2_BUCKET_NAME        - the target bucket's name\n"
            "  R2_PUBLIC_BASE_URL    - the bucket's public r2.dev base URL"
        )
        return
    finally:
        local_path.unlink(missing_ok=True)

    print(f"Uploaded to key: {result['key']}")
    print(f"Public URL: {result['public_url']}")
    print("Fetch that URL in a browser to confirm the bucket is public.")

    delete_media(result["key"])
    print("Deleted test file. R2 storage is working correctly.")


if __name__ == "__main__":
    main()
