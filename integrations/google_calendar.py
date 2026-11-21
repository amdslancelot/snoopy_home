import asyncio
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Optional
from zoneinfo import ZoneInfo

from config import settings
from core.observability import get_logger

log = get_logger("calendar")

# How far ahead update/delete search when the user names no date.
_SEARCH_DAYS_AHEAD = 365


def _best_title_match(query: str, items: list[dict]) -> Optional[dict]:
    """Return the best-matching event by title, or None if similarity < 0.6."""
    if not items:
        return None
    q = query.lower()
    best, best_score = None, 0.0
    for item in items:
        c = item.get("summary", "").lower()
        if q == c:
            return item
        score = 0.9 if (q in c or c in q) else SequenceMatcher(None, q, c).ratio()
        if score > best_score:
            best_score, best = score, item
    return best if best_score >= 0.6 else None


class GoogleCalendarClient:
    def __init__(self):
        self._service = None
        self._calendar_timezone: Optional[str] = None

    def _get_service(self):
        if self._service is not None:
            return self._service
        if not settings.google_service_account_json or not settings.household_calendar_id:
            return None
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                settings.google_service_account_json,
                scopes=["https://www.googleapis.com/auth/calendar"],
            )
            self._service = build("calendar", "v3", credentials=creds)
            try:
                cal = self._service.calendars().get(
                    calendarId=settings.household_calendar_id
                ).execute()
                self._calendar_timezone = cal.get("timeZone")
                log.info("timezone_detected", timezone=self._calendar_timezone)
            except Exception as exc:
                log.warning("timezone_fetch_failed", error=str(exc))
            return self._service
        except Exception as exc:
            log.error("service_build_failed", error=str(exc))
            return None

    @property
    def timezone(self) -> str:
        return self._calendar_timezone or settings.timezone or "UTC"

    def _local(self, dt: datetime) -> datetime:
        """Attach the calendar timezone to a naive datetime."""
        try:
            return dt.replace(tzinfo=ZoneInfo(self.timezone))
        except Exception:
            return dt

    async def _find_event(self, title: str, start: Optional[datetime]) -> Optional[dict]:
        """Search the calendar and return the best title-matching event, or None."""
        service = self._get_service()
        if not service:
            return None

        if start:
            start_local = self._local(start)
            time_min = (start_local - timedelta(hours=24)).isoformat()
            time_max = (start_local + timedelta(hours=24)).isoformat()
        else:
            # No date given: search from yesterday to a year out. A 7-day
            # window made "delete <event>" fail for anything booked further
            # ahead (e.g. a January hotel stay asked about in November).
            now = datetime.utcnow()
            time_min = (now - timedelta(days=1)).isoformat() + "Z"
            time_max = (now + timedelta(days=_SEARCH_DAYS_AHEAD)).isoformat() + "Z"

        loop = asyncio.get_running_loop()
        try:
            log.debug("event_search", title=title, time_min=time_min, time_max=time_max)
            result = await loop.run_in_executor(
                None,
                lambda: service.events()
                .list(
                    calendarId=settings.household_calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=2500,
                )
                .execute(),
            )
            items = result.get("items", [])
            log.debug("events_found", count=len(items), titles=[e.get("summary") for e in items])
            return _best_title_match(title, items)
        except Exception as exc:
            log.error("find_event_failed", title=title, error=str(exc))
            return None

    async def list_events(self, days_ahead: int = 7) -> Optional[list[dict]]:
        """Upcoming events in the next `days_ahead` days; None when unconfigured."""
        service = self._get_service()
        if not service:
            return None

        now = datetime.utcnow()
        time_min = now.isoformat() + "Z"
        time_max = (now + timedelta(days=days_ahead)).isoformat() + "Z"

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: service.events()
                .list(
                    calendarId=settings.household_calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute(),
            )
            return [
                {
                    "title": e.get("summary", ""),
                    "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
                    "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
                }
                for e in result.get("items", [])
            ]
        except Exception as exc:
            log.error("list_events_failed", error=str(exc))
            return []

    async def create_event(
        self,
        title: str,
        description: str,
        start: datetime,
        end: Optional[datetime],
        attendee_emails: list[str],
        location: Optional[str] = None,
        timezone: Optional[str] = None,
        all_day: bool = False,
    ) -> Optional[dict]:
        """Insert the event. Returns {"invites_sent": bool} on success, None on failure.

        A service account on a consumer (@gmail.com) calendar cannot invite
        attendees — Google returns 403 "Service accounts cannot invite attendees
        without Domain-Wide Delegation of Authority" (DWD exists only on Google
        Workspace). On that error the event is re-created without attendees and
        their addresses are noted in the description instead.
        """
        service = self._get_service()
        if not service:
            return None

        tz = self.timezone
        if timezone:
            try:
                ZoneInfo(timezone)
                tz = timezone
            except Exception:
                log.warning("invalid_event_timezone", timezone=timezone, fallback=tz)

        if all_day:
            # Google all-day events use `date` with an EXCLUSIVE end, while the
            # model sends the last day itself (check-out Jan 18 → end Jan 18).
            # Add a day so the last day shows on the calendar.
            last_day = max((end or start).date(), start.date())
            end_date = last_day + timedelta(days=1)
            start_field = {"date": start.date().isoformat()}
            end_field = {"date": end_date.isoformat()}
        else:
            if end is None:
                end = start + timedelta(hours=1)
            start_field = {"dateTime": start.isoformat(), "timeZone": tz}
            end_field = {"dateTime": end.isoformat(), "timeZone": tz}

        body = {
            "summary": title,
            "description": description,
            "start": start_field,
            "end": end_field,
            "attendees": [{"email": e} for e in attendee_emails if e],
        }
        if location:
            body["location"] = location

        loop = asyncio.get_running_loop()

        def _insert(b: dict):
            return (
                service.events()
                .insert(calendarId=settings.household_calendar_id, body=b, sendUpdates="all")
                .execute()
            )

        try:
            await loop.run_in_executor(None, lambda: _insert(body))
            return {"invites_sent": bool(body["attendees"])}
        except Exception as exc:
            if body["attendees"] and "Domain-Wide Delegation" in str(exc):
                emails = [a["email"] for a in body["attendees"]]
                log.warning("invites_unsupported_retrying_without", title=title, attendees=emails)
                retry = {**body, "attendees": []}
                note = "Attendees (invites not supported by this calendar): " + ", ".join(emails)
                retry["description"] = f"{description}\n\n{note}" if description else note
                try:
                    await loop.run_in_executor(None, lambda: _insert(retry))
                    return {"invites_sent": False}
                except Exception as exc2:
                    exc = exc2
            log.error("create_event_failed", title=title, error=str(exc))
            return None

    async def update_event(
        self,
        title: str,
        start: Optional[datetime],
        new_title: Optional[str] = None,
        new_start: Optional[datetime] = None,
        new_end: Optional[datetime] = None,
        new_description: Optional[str] = None,
    ) -> Optional[str]:
        """Patch the best fuzzy title match. Returns the MATCHED event's title
        (which may differ from `title`), or None if nothing was updated."""
        service = self._get_service()
        if not service:
            return None

        match = await self._find_event(title, start)
        if not match:
            log.warning("update_event_not_found", title=title)
            return None
        matched_title = match.get("summary", "")

        event_id = match["id"]
        tz = self.timezone
        patch: dict = {}

        if new_title is not None:
            patch["summary"] = new_title
        if new_description is not None:
            patch["description"] = new_description
        if new_start is not None:
            new_start_local = self._local(new_start)
            patch["start"] = {"dateTime": new_start_local.isoformat(), "timeZone": tz}
            # Preserve original duration when only start changes
            if new_end is None:
                try:
                    orig_s = datetime.fromisoformat(match["start"]["dateTime"])
                    orig_e = datetime.fromisoformat(match["end"]["dateTime"])
                    patch["end"] = {
                        "dateTime": (new_start_local + (orig_e - orig_s)).isoformat(),
                        "timeZone": tz,
                    }
                except Exception:
                    patch["end"] = {
                        "dateTime": (new_start_local + timedelta(hours=1)).isoformat(),
                        "timeZone": tz,
                    }
        if new_end is not None:
            patch["end"] = {"dateTime": self._local(new_end).isoformat(), "timeZone": tz}

        if not patch:
            return matched_title

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: service.events()
                .patch(
                    calendarId=settings.household_calendar_id,
                    eventId=event_id,
                    body=patch,
                )
                .execute(),
            )
            log.info("event_updated", event_id=event_id, title=matched_title, fields=list(patch.keys()))
            return matched_title
        except Exception as exc:
            log.error("update_event_failed", title=title, error=str(exc))
            return None

    async def delete_event(self, title: str, start: Optional[datetime]) -> Optional[str]:
        """Delete the best fuzzy title match. Returns the MATCHED event's title
        (which may differ from `title`), or None if nothing was deleted."""
        service = self._get_service()
        if not service:
            return None

        match = await self._find_event(title, start)
        if not match:
            log.warning("delete_event_not_found", title=title)
            return None

        event_id = match["id"]
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: service.events()
                .delete(
                    calendarId=settings.household_calendar_id,
                    eventId=event_id,
                )
                .execute(),
            )
            log.info("event_deleted", event_id=event_id, title=match.get("summary"))
            return match.get("summary", "")
        except Exception as exc:
            log.error("delete_event_failed", title=title, error=str(exc))
            return None


google_calendar = GoogleCalendarClient()
