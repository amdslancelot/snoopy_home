"""Unit tests for google_calendar.py helpers that don't require API access."""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from integrations.google_calendar import _best_title_match, GoogleCalendarClient


class TestBestTitleMatch:
    def test_exact_match(self):
        items = [{"summary": "Dentist appointment"}]
        result = _best_title_match("dentist appointment", items)
        assert result is not None
        assert result["summary"] == "Dentist appointment"

    def test_case_insensitive_match(self):
        items = [{"summary": "Yoga with Amugi"}]
        assert _best_title_match("YOGA WITH AMUGI", items) is not None

    def test_substring_match(self):
        items = [{"summary": "Yoga with Amugi"}]
        assert _best_title_match("yoga", items) is not None

    def test_no_match_below_threshold(self):
        items = [{"summary": "Yoga with Amugi"}]
        assert _best_title_match("dentist xyz completely different", items) is None

    def test_empty_items_returns_none(self):
        assert _best_title_match("anything", []) is None

    def test_returns_best_of_multiple(self):
        items = [
            {"summary": "Yoga class"},
            {"summary": "Yoga with Amugi"},
            {"summary": "Dentist"},
        ]
        result = _best_title_match("yoga with amugi", items)
        assert result["summary"] == "Yoga with Amugi"

    def test_partial_query_in_summary(self):
        items = [{"summary": "Team meeting 2026"}]
        result = _best_title_match("team meeting", items)
        assert result is not None


class TestGoogleCalendarClientLocal:
    def test_local_attaches_timezone(self):
        client = GoogleCalendarClient()
        client._calendar_timezone = "Asia/Taipei"
        naive = datetime(2026, 6, 20, 9, 0, 0)
        local = client._local(naive)
        assert local.tzinfo is not None

    def test_timezone_property_falls_back_to_settings(self):
        client = GoogleCalendarClient()
        client._calendar_timezone = None
        # Should return settings.timezone or "UTC"
        tz = client.timezone
        assert isinstance(tz, str)
        assert len(tz) > 0

    def test_timezone_property_prefers_calendar_timezone(self):
        client = GoogleCalendarClient()
        client._calendar_timezone = "Europe/London"
        assert client.timezone == "Europe/London"
