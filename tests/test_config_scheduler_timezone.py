"""
Tests for SCHEDULER_TIMEZONE / next_slot_for's timezone handling (Phase 20).

Timezone-conversion cases monkeypatch app.config.SCHEDULER_TIMEZONE directly
(next_slot_for reads it as a module global at call time) instead of
re-importing app.config with a different SCHEDULER_TIMEZONE env var, since
this process already has app.config (and everything that imports it)
loaded once for the whole test session — re-parsing it via importlib.reload
would re-run the whole module, including re-instantiating the frozen
Settings singleton other tests hold a reference to.

The "invalid timezone name fails at startup" case genuinely needs a fresh
interpreter (the failure happens at module import time), so it shells out
via subprocess instead.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import config

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestNextSlotForDefaultUtc:
    def test_default_scheduler_timezone_is_utc(self):
        # No SCHEDULER_TIMEZONE is set anywhere in the test environment
        # (see tests/conftest.py), so this is what a fresh import produces.
        assert config.SCHEDULER_TIMEZONE == ZoneInfo("UTC")

    def test_slot_in_utc_matches_wall_clock(self):
        # youtube's single default slot is 15:00.
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)

        result = config.next_slot_for("youtube", now)

        assert result == datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)
        assert result.tzinfo is not None

    def test_naive_now_is_treated_as_utc(self):
        now_naive = datetime(2026, 6, 1, 10, 0)

        result = config.next_slot_for("youtube", now_naive)

        assert result == datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)


class TestNextSlotForNonUtcTimezone:
    def test_slot_converts_from_local_wall_clock_to_utc(self, monkeypatch):
        # UTC-3, fixed offset (no DST), so the math below is unambiguous.
        monkeypatch.setattr(config, "SCHEDULER_TIMEZONE", ZoneInfo("America/Argentina/Buenos_Aires"))

        # 09:00 local hasn't happened yet -> today's first slot, in UTC.
        now = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)  # 05:00 local

        result = config.next_slot_for("twitter", now)

        # 09:00 local == 12:00 UTC (local + 3h).
        assert result == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def test_slot_computed_in_local_date_crosses_utc_midnight(self, monkeypatch):
        # `now`'s UTC date and its Buenos Aires local date disagree here
        # (01:30 UTC on Mar 1 is 22:30 local on Feb 28) — this asserts the
        # local date is what's used to decide "today's slots already
        # passed", not the UTC date the naive pre-Phase-20 code would have
        # implicitly assumed.
        monkeypatch.setattr(config, "SCHEDULER_TIMEZONE", ZoneInfo("America/Argentina/Buenos_Aires"))
        now = datetime(2026, 3, 1, 1, 30, tzinfo=timezone.utc)

        result = config.next_slot_for("twitter", now)

        # All of today's (Feb 28 local) slots (09/13/18) have passed by
        # 22:30 local -> next is tomorrow's (Mar 1 local) first slot, 09:00
        # local == 12:00 UTC.
        assert result == datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def test_aware_now_in_a_third_timezone_is_normalized_correctly(self, monkeypatch):
        monkeypatch.setattr(config, "SCHEDULER_TIMEZONE", ZoneInfo("America/Argentina/Buenos_Aires"))
        # 2026-06-01T08:00:00+05:00 == 2026-06-01T03:00:00Z == 2026-06-01T00:00:00-03:00 local.
        now = datetime(2026, 6, 1, 8, 0, tzinfo=timezone(__import__("datetime").timedelta(hours=5)))

        result = config.next_slot_for("twitter", now)

        assert result == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


class TestSchedulerTimezoneValidation:
    def test_invalid_timezone_name_fails_fast_at_import(self):
        env = os.environ.copy()
        env["SCHEDULER_TIMEZONE"] = "Not/AZone"

        result = subprocess.run(
            [sys.executable, "-c", "import app.config"],
            cwd=str(_PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode != 0
        assert "SCHEDULER_TIMEZONE" in result.stderr
        assert "Not/AZone" in result.stderr
