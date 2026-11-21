"""Household-local ↔ UTC conversion (core/timeutil)."""

from datetime import datetime, timedelta, timezone

from core.timeutil import local_to_utc, to_household_local


def test_naive_local_converts_to_utc():
    # LA in November is PST (UTC-8).
    assert local_to_utc(datetime(2026, 11, 20, 21, 0), "America/Los_Angeles") == datetime(2026, 11, 21, 5, 0)


def test_dst_is_respected():
    # LA in July is PDT (UTC-7).
    assert local_to_utc(datetime(2026, 7, 18, 9, 0), "America/Los_Angeles") == datetime(2026, 7, 18, 16, 0)


def test_aware_input_uses_its_own_offset():
    aware = datetime(2026, 11, 20, 21, 0, tzinfo=timezone(timedelta(hours=8)))
    assert local_to_utc(aware, "America/Los_Angeles") == datetime(2026, 11, 20, 13, 0)


def test_to_household_local_converts_aware_and_keeps_naive():
    aware = datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc)
    assert to_household_local(aware, "America/Los_Angeles") == datetime(2026, 11, 20, 21, 0)
    naive = datetime(2026, 11, 20, 21, 0)
    assert to_household_local(naive, "America/Los_Angeles") == naive


def test_bad_tz_falls_back_to_utc():
    assert local_to_utc(datetime(2026, 1, 1, 0, 0), "Not/AZone") == datetime(2026, 1, 1, 0, 0)


def test_utc_to_local_round_trips():
    utc = datetime(2026, 11, 21, 5, 0)
    from core.timeutil import utc_to_local
    assert utc_to_local(utc, "America/Los_Angeles") == datetime(2026, 11, 20, 21, 0)
    assert local_to_utc(utc_to_local(utc, "Asia/Taipei"), "Asia/Taipei") == utc
