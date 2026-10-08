"""
Tests for publish_job's success-status branch (Phase 34): a publisher's
result dict can signal result["requires_user_action"]=True to mean "this
succeeded, but it isn't actually live yet — the account owner still has to
finish it on the platform's own app" (first/only real user today:
app/publishers/tiktok.py's inbox-upload flow, which lands a video as a
draft in the account's TikTok inbox because this app only holds the
video.upload permission, not video.publish/Direct Post — not a Sandbox
limitation, see that module's docstring). publish_job must persist
JobStatus.NEEDS_USER_ACTION in that case instead of PUBLISHED, and keep
persisting plain PUBLISHED for every publisher that doesn't set the flag.

The publisher itself is monkeypatched into app.tasks._PUBLISHERS_BY_PLATFORM
(same pattern as tests/test_tasks_facebook_token_refresh.py and friends), so
this exercises only app/tasks.py's own branching logic — no real HTTP, no
Celery worker/broker, called directly against the throwaway SQLite DB from
tests/conftest.py.
"""

from app import tasks
from app.models import Job, JobStatus


def _make_job(db_session, platform):
    job = Job(platform=platform, payload={}, status=JobStatus.QUEUED)
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


class TestRequiresUserActionFlag:
    def test_requires_user_action_true_sets_needs_user_action(self, db_session, monkeypatch):
        job = _make_job(db_session, "tiktok")
        monkeypatch.setitem(
            tasks._PUBLISHERS_BY_PLATFORM,
            "tiktok",
            lambda platform, payload, account_credentials: {
                "platform": "tiktok",
                "external_id": "pub-123",
                "requires_user_action": True,
            },
        )

        tasks.publish_job(job.id)

        db_session.refresh(job)
        assert job.status == JobStatus.NEEDS_USER_ACTION
        assert job.external_id == "pub-123"

    def test_flag_absent_still_sets_published(self, db_session, monkeypatch):
        # Every other publisher in this project doesn't set the flag at
        # all — the default, unconditional PUBLISHED outcome from before
        # this phase must be unchanged for them.
        job = _make_job(db_session, "youtube")
        monkeypatch.setitem(
            tasks._PUBLISHERS_BY_PLATFORM,
            "youtube",
            lambda platform, payload, account_credentials: {"platform": "youtube", "external_id": "vid-1"},
        )

        tasks.publish_job(job.id)

        db_session.refresh(job)
        assert job.status == JobStatus.PUBLISHED

    def test_flag_false_sets_published(self, db_session, monkeypatch):
        # A publisher that completed a direct, live publish but still
        # includes the key (e.g. explicitly False) must not be mistaken for
        # one that needs the account owner's action.
        job = _make_job(db_session, "tiktok")
        monkeypatch.setitem(
            tasks._PUBLISHERS_BY_PLATFORM,
            "tiktok",
            lambda platform, payload, account_credentials: {
                "platform": "tiktok",
                "external_id": "pub-456",
                "requires_user_action": False,
            },
        )

        tasks.publish_job(job.id)

        db_session.refresh(job)
        assert job.status == JobStatus.PUBLISHED
