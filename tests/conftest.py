"""
Shared pytest fixtures.

DATABASE_URL is pointed at a throwaway SQLite file *before* any app module
is imported, since app/config.py reads it into a frozen Settings instance at
import time and fails loudly if it's unset — this keeps the whole test suite
away from the real Neon database without touching app/config.py or app/db.py.
Every test that needs a working DB gets one via the `_clean_tables` autouse
fixture, which resets all tables after each test (tasks under test open
their own SessionLocal() sessions internally, so a rollback-only fixture
wouldn't undo their commits).

No test in this suite makes a real network call: HTTP is mocked with
`responses` or monkeypatched, and ALERT_WEBHOOK_URL / TIKTOK_WEBHOOK_SKIP_SIGNATURE
are cleared here so nothing accidentally fires a real webhook.
"""

import os
import tempfile
from pathlib import Path

_TEST_DB_PATH = Path(tempfile.gettempdir()) / "distribution_engine_test.db"
if _TEST_DB_PATH.exists():
    _TEST_DB_PATH.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ.setdefault("ALERT_WEBHOOK_URL", "")
os.environ.setdefault("TIKTOK_WEBHOOK_SKIP_SIGNATURE", "")
# A real .env can set SCHEDULER_TIMEZONE (e.g. for local/prod scheduling).
# app/config.py's load_dotenv() call defaults to override=False, meaning it
# only fills in variables NOT ALREADY in os.environ — so popping this
# wouldn't help (dotenv would just refill it from .env on import); setting
# it explicitly here, before the first `from app...` import anywhere in the
# suite (including this file's own, below), is what actually pins it to the
# documented UTC default. Same pattern as ALERT_WEBHOOK_URL/
# TIKTOK_WEBHOOK_SKIP_SIGNATURE below, just an explicit set instead of
# setdefault since we want to unconditionally win over any real .env value,
# not just fill in a gap.
os.environ["SCHEDULER_TIMEZONE"] = "UTC"
# Pinned so app/auth.py doesn't fall back to a random per-process key (which
# would still work, but would also print its loud startup warning on every
# test run for no reason) and so session tokens are reproducible if a test
# ever needs to hand-construct one.
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret-not-for-production")
# Enables dashboard/api.py's Basic-Auth ("ops" admin) gate for the whole
# suite, same reasoning as SESSION_SECRET_KEY above: without this,
# dashboard/api.py's dev-mode bypass (_AUTH_ENABLED=False) would make every
# request act as admin regardless of session state, which would silently
# defeat tests/test_dashboard_auth.py's scoping/middleware assertions.
os.environ.setdefault("DASHBOARD_USERNAME", "test-admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "test-admin-password")
# FastAPI's TestClient talks to "http://testserver" (plain HTTP), so a
# Secure-flagged session cookie (the production default) would never be
# stored/resent by its cookie jar — same reason a real browser wouldn't
# send one back over http://localhost either. Local/test-only, same as the
# loud warning this triggers in dashboard/api.py.
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.models import Account, Job, JobStatus, User, WebhookEvent  # noqa: E402,F401


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    if _TEST_DB_PATH.exists():
        _TEST_DB_PATH.unlink()


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    session = SessionLocal()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    finally:
        session.close()


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
