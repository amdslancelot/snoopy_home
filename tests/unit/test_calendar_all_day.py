"""All-day events: the model sends the LAST day; Google wants an exclusive end."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from integrations.google_calendar import GoogleCalendarClient


@pytest.fixture
def client_and_body():
    client = GoogleCalendarClient()
    service = MagicMock()
    client._service = service
    client._calendar_timezone = "America/Los_Angeles"
    captured = {}

    def insert(calendarId, body, sendUpdates):
        captured["body"] = body
        return MagicMock(execute=MagicMock(return_value={}))

    service.events.return_value.insert.side_effect = insert
    return client, captured


@pytest.mark.asyncio
async def test_multi_day_stay_includes_checkout_day(client_and_body):
    client, captured = client_and_body
    ok = await client.create_event(
        "Hyatt stay", "", datetime(2027, 1, 15), datetime(2027, 1, 18), [], all_day=True
    )
    assert ok
    assert captured["body"]["start"] == {"date": "2027-01-15"}
    assert captured["body"]["end"] == {"date": "2027-01-19"}  # exclusive → Jan 18 shows


@pytest.mark.asyncio
async def test_single_all_day_event(client_and_body):
    client, captured = client_and_body
    await client.create_event("Presentation", "", datetime(2027, 1, 16), None, [], all_day=True)
    assert captured["body"]["start"] == {"date": "2027-01-16"}
    assert captured["body"]["end"] == {"date": "2027-01-17"}


@pytest.mark.asyncio
async def test_end_before_start_collapses_to_one_day(client_and_body):
    client, captured = client_and_body
    await client.create_event("x", "", datetime(2027, 1, 16), datetime(2027, 1, 10), [], all_day=True)
    assert captured["body"]["end"] == {"date": "2027-01-17"}


@pytest.mark.asyncio
async def test_find_event_without_date_searches_a_year_ahead():
    # Regression: "delete <event>" with no date only looked 7 days ahead, so a
    # January stay couldn't be deleted in November.
    client = GoogleCalendarClient()
    service = MagicMock()
    client._service = service
    seen = {}

    def list_(**kw):
        seen.update(kw)
        return MagicMock(execute=MagicMock(return_value={"items": [
            {"id": "e1", "summary": "Hyatt Desert Oasis Reservation"}]}))

    service.events.return_value.list.side_effect = list_
    match = await client._find_event("Hyatt Desert Oasis Reservation", None)
    assert match["id"] == "e1"
    span = datetime.fromisoformat(seen["timeMax"].rstrip("Z")) - datetime.fromisoformat(seen["timeMin"].rstrip("Z"))
    assert span.days >= 365


@pytest.mark.asyncio
async def test_delete_reports_the_matched_title_not_the_requested_one():
    # Regression: asking to delete "Hyatt Desert Oasis Reservation" fuzzy-matched
    # and deleted "Hyatt Vacation Club at Desert Oasis Stay", but the result
    # echoed the requested name, hiding the substitution.
    client = GoogleCalendarClient()
    service = MagicMock()
    client._service = service
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "e2", "summary": "Hyatt Vacation Club at Desert Oasis Stay"}]}
    service.events.return_value.delete.return_value.execute.return_value = {}
    deleted = await client.delete_event("Hyatt Desert Oasis Stay", None)
    assert deleted == "Hyatt Vacation Club at Desert Oasis Stay"


@pytest.mark.asyncio
async def test_delete_returns_none_when_nothing_matches():
    client = GoogleCalendarClient()
    service = MagicMock()
    client._service = service
    service.events.return_value.list.return_value.execute.return_value = {"items": []}
    assert await client.delete_event("nope", None) is None


@pytest.mark.asyncio
async def test_invite_403_retries_without_attendees():
    # A service account on a @gmail.com calendar can't invite: Google 403s with
    # "Domain-Wide Delegation". The event must still be created.
    client = GoogleCalendarClient()
    service = MagicMock()
    client._service = service
    client._calendar_timezone = "America/Los_Angeles"
    bodies = []

    def insert(calendarId, body, sendUpdates):
        bodies.append(body)
        if body["attendees"]:
            raise Exception("Service accounts cannot invite attendees without Domain-Wide Delegation of Authority.")
        return MagicMock(execute=MagicMock(return_value={}))

    service.events.return_value.insert.side_effect = insert
    result = await client.create_event(
        "Stay", "Conf 123", datetime(2027, 1, 15), datetime(2027, 1, 18), ["a@example.com"], all_day=True)
    assert result == {"invites_sent": False}
    assert bodies[-1]["attendees"] == []
    assert "a@example.com" in bodies[-1]["description"] and "Conf 123" in bodies[-1]["description"]


@pytest.mark.asyncio
async def test_other_errors_still_fail():
    client = GoogleCalendarClient()
    service = MagicMock()
    client._service = service
    service.events.return_value.insert.side_effect = Exception("500 backend error")
    assert await client.create_event("x", "", datetime(2027, 1, 15), None, ["a@example.com"]) is None
