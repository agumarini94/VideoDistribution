"""
Queries TikTok's real post-status endpoint (POST /v2/post/publish/status/fetch/)
for a given publish_id, via app/publishers/tiktok.py::fetch_post_status.

Why this exists: publish_job marks a TikTok job PUBLISHED as soon as the
chunked upload's PUT requests all ack 2xx (see app/publishers/tiktok.py's
publish()) — that only confirms TikTok received the bytes, not that it
finished processing them, and (Sandbox/inbox mode) not that the account
owner has actually opened the TikTok app and posted the draft. Nothing in
this project ever calls the status-fetch endpoint otherwise, so a job
sitting at PUBLISHED can still be unfinished, rejected, or just waiting
untouched in the user's TikTok inbox — this script is the way to check the
real status by hand.

Accepts either --job-id (looks up the Job, reads its stored publish_id from
Job.external_id — see app/tasks.py::_persist_external_id — and its
account_id to resolve credentials) or --publish-id + --account directly
(e.g. to check a publish_id that predates external_id being persisted, or
one obtained outside this project).

Run it from the project root with:
    python -m scripts.check_tiktok_status --job-id 42
    python -m scripts.check_tiktok_status --publish-id v_inbox.XXXX --account "Main account"
"""

import argparse
import json

from app.db import SessionLocal
from app.models import Account, Job
from app.publishers.tiktok import fetch_post_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job-id", type=int, metavar="ID", help="Look up publish_id/account from this Job's row.")
    parser.add_argument(
        "--publish-id",
        metavar="PUBLISH_ID",
        help="Check this publish_id directly (requires --account, since there's no Job to resolve credentials from).",
    )
    parser.add_argument(
        "--account",
        metavar="NAME",
        help='TikTok Account name (platform="tiktok") to use for the bearer token. '
        "Required with --publish-id; with --job-id, overrides the job's own account_id if given.",
    )
    args = parser.parse_args()

    if not args.job_id and not args.publish_id:
        parser.error("Pass either --job-id or --publish-id.")
    if args.publish_id and not args.account:
        parser.error("--publish-id requires --account (no Job to resolve credentials from).")
    return args


def _resolve_account(db, name: str) -> Account:
    account = db.query(Account).filter(Account.platform == "tiktok", Account.name == name).one_or_none()
    if account is None:
        raise SystemExit(f'No tiktok Account named {name!r}.')
    return account


def main() -> None:
    args = parse_args()

    db = SessionLocal()
    try:
        publish_id = args.publish_id
        account = None

        if args.job_id:
            job = db.get(Job, args.job_id)
            if job is None:
                raise SystemExit(f"No job #{args.job_id}.")
            if job.platform != "tiktok":
                raise SystemExit(f"Job #{args.job_id} is platform={job.platform!r}, not tiktok.")
            if not publish_id:
                if not job.external_id:
                    raise SystemExit(
                        f"Job #{args.job_id} has no external_id (publish_id) stored — "
                        "either it was never published, or it predates Phase 10b. Pass --publish-id directly instead."
                    )
                publish_id = job.external_id
            if args.account:
                account = _resolve_account(db, args.account)
            elif job.account_id is not None:
                account = db.get(Account, job.account_id)
                if account is None:
                    raise SystemExit(f"Job #{args.job_id} references account_id={job.account_id}, which does not exist.")
            else:
                raise SystemExit(
                    f"Job #{args.job_id} has no account_id (TikTok has no single-account fallback) — pass --account."
                )
        else:
            account = _resolve_account(db, args.account)

        access_token = str((account.credentials or {}).get("access_token", "")).strip()
        if not access_token:
            raise SystemExit(f"Account {account.name!r} has no access_token stored — re-run scripts/authorize_tiktok.py.")

        print(f"Checking publish_id={publish_id!r} via account {account.name!r}...")
        data = fetch_post_status(access_token, publish_id)
        print(json.dumps(data, indent=2))

        status = data.get("status")
        if status == "PUBLISH_COMPLETE":
            print("\n-> Live/complete.")
        elif status == "SEND_TO_USER_INBOX":
            print("\n-> Uploaded to the user's TikTok inbox as a draft (Sandbox/inbox mode) — "
                  "they still have to open the app and post it manually. This is why no "
                  "notification arrived even though our own job is PUBLISHED.")
        elif status == "FAILED":
            print(f"\n-> FAILED. fail_reason: {data.get('fail_reason', '(none given)')}")
        elif status in ("PROCESSING_DOWNLOAD", "PROCESSING_UPLOAD"):
            print("\n-> Still processing on TikTok's side — check again shortly.")
        elif status:
            print(f"\n-> Unrecognized status {status!r} — treat defensively, see fetch_post_status's docstring.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
