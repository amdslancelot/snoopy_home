"""Household-local ↔ UTC conversion for datetimes the LLM emits.

Contract: every datetime in a tool argument is the household's LOCAL
wall-clock time (the model sees both clocks in `_stamp_date`, and users speak
in local time — "at 9 pm"). Storage and the scheduler use naive UTC, so the
conversion happens exactly once, at the handler boundary, via these helpers.

The household timezone is `google_calendar.timezone` — the same source
`_stamp_date` uses — so the clock the model reads and the clock we convert
with can never disagree.
"""

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from integrations.google_calendar import google_calendar


def household_tz(tz: Optional[str] = None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or google_calendar.timezone)
    except Exception:
        return ZoneInfo("UTC")


def to_household_local(dt: datetime, tz: Optional[str] = None) -> datetime:
    """Naive household-local datetime. Naive input is assumed local already;
    aware input (e.g. an explicit `+08:00` offset) is converted."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(household_tz(tz)).replace(tzinfo=None)


def utc_to_local(dt: datetime, tz: Optional[str] = None) -> datetime:
    """Naive household-local datetime from a naive-UTC one (storage → user)."""
    return dt.replace(tzinfo=timezone.utc).astimezone(household_tz(tz)).replace(tzinfo=None)


def local_to_utc(dt: datetime, tz: Optional[str] = None) -> datetime:
    """Naive UTC datetime from a household-local (naive) or aware datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=household_tz(tz))
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
