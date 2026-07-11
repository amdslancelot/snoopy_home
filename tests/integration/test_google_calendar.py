"""
Live integration tests against the real Google Calendar API.

These tests CREATE, UPDATE, and DELETE real calendar events. They require:
  - GOOGLE_SERVICE_ACCOUNT_JSON path set in .env
  - HOUSEHOLD_CALENDAR_ID set in .env

They clean up after themselves (delete what they create).

Run with:
    pytest -m live tests/integration/test_google_calendar.py -v
"""

import os
import pytest
from datetime import datetime, timedelta

pytestmark = pytest.mark.live

from config import settings

_CONFIGURED = bool(
    settings.google_service_account_json
    and settings.household_calendar_id
)
skip_if_not_configured = pytest.mark.skipif(
    not _CONFIGURED,
    reason="Requires GOOGLE_SERVICE_ACCOUNT_JSON and HOUSEHOLD_CALENDAR_ID in .env",
)

# Use events far in the future so they don't interfere with real calendar use
_BASE_START = datetime.utcnow() + timedelta(days=30)


@skip_if_not_configured
class TestGoogleCalendarLive:
    @pytest.fixture
    def cal(self):
        from integrations.google_calendar import GoogleCalendarClient
        return GoogleCalendarClient()

    async def test_create_and_find_event(self, cal):
        title = "Snoopy Test — create_find — delete me"
        start = _BASE_START.replace(hour=10, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)

        created = await cal.create_event(title, "automated test", start, end, [])
        assert created, "create_event returned False"

        found = await cal._find_event(title, start)
        assert found is not None, "event not found after creation"
        assert title.lower() in found["summary"].lower()

        # Cleanup
        await cal.delete_event(title, start)

    async def test_delete_event(self, cal):
        title = "Snoopy Test — delete — delete me"
        start = _BASE_START.replace(hour=11, minute=0, second=0, microsecond=0)

        await cal.create_event(title, "", start, None, [])
        deleted = await cal.delete_event(title, start)
        assert deleted, "delete_event returned False"

        found = await cal._find_event(title, start)
        assert found is None, "event still found after deletion"

    async def test_update_event_reschedule(self, cal):
        title = "Snoopy Test — reschedule — delete me"
        start = _BASE_START.replace(hour=12, minute=0, second=0, microsecond=0)
        new_start = start + timedelta(hours=2)

        await cal.create_event(title, "", start, None, [])

        updated = await cal.update_event(title, start, new_start=new_start)
        assert updated, "update_event returned False"

        # Verify event moved to new time
        found = await cal._find_event(title, new_start)
        assert found is not None, "event not found at new time after reschedule"

        # Cleanup
        await cal.delete_event(title, new_start)

    async def test_update_event_rename(self, cal):
        original_title = "Snoopy Test — rename original — delete me"
        new_title = "Snoopy Test — rename updated — delete me"
        start = _BASE_START.replace(hour=13, minute=0, second=0, microsecond=0)

        await cal.create_event(original_title, "", start, None, [])

        updated = await cal.update_event(original_title, start, new_title=new_title)
        assert updated, "update_event rename returned False"

        found = await cal._find_event(new_title, start)
        assert found is not None, "event not found under new title after rename"

        # Cleanup
        await cal.delete_event(new_title, start)

    async def test_find_event_fuzzy_title_match(self, cal):
        title = "Snoopy Fuzzy Match Integration Test"
        start = _BASE_START.replace(hour=14, minute=0, second=0, microsecond=0)

        await cal.create_event(title, "", start, None, [])

        # Search with partial title
        found = await cal._find_event("snoopy fuzzy match", start)
        assert found is not None, "fuzzy title match failed"

        # Cleanup
        await cal.delete_event(title, start)

    async def test_event_not_found_returns_none(self, cal):
        result = await cal._find_event("this event definitely does not exist xyz123", None)
        assert result is None

    async def test_update_nonexistent_event_returns_false(self, cal):
        result = await cal.update_event(
            "nonexistent xyz123 event", None, new_title="something"
        )
        assert result is False

    async def test_delete_nonexistent_event_returns_false(self, cal):
        result = await cal.delete_event("nonexistent xyz123 event", None)
        assert result is False
