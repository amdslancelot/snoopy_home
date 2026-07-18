import asyncio
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Optional
from zoneinfo import ZoneInfo

from config import settings
from core.observability import get_logger

log = get_logger("calendar")


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
            now = datetime.utcnow()
            time_min = now.isoformat() + "Z"
            time_max = (now + timedelta(days=7)).isoformat() + "Z"

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
                )
                .execute(),
            )
            items = result.get("items", [])
            log.debug("events_found", count=len(items), titles=[e.get("summary") for e in items])
            return _best_title_match(title, items)
        except Exception as exc:
            log.error("find_event_failed", title=title, error=str(exc))
            return None

    async def create_event(
        self,
        title: str,
        description: str,
        start: datetime,
        end: Optional[datetime],
        attendee_emails: list[str],
    ) -> bool:
        service = self._get_service()
        if not service:
            return False

        tz = self.timezone
        if end is None:
            end = start + timedelta(hours=1)

        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
            "attendees": [{"email": e} for e in attendee_emails if e],
        }

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: service.events()
                .insert(
                    calendarId=settings.household_calendar_id,
                    body=body,
                    sendUpdates="all",
                )
                .execute(),
            )
            return True
        except Exception as exc:
            log.error("create_event_failed", title=title, error=str(exc))
            return False

    async def update_event(
        self,
        title: str,
        start: Optional[datetime],
        new_title: Optional[str] = None,
        new_start: Optional[datetime] = None,
        new_end: Optional[datetime] = None,
        new_description: Optional[str] = None,
    ) -> bool:
        service = self._get_service()
        if not service:
            return False

        match = await self._find_event(title, start)
        if not match:
            log.warning("update_event_not_found", title=title)
            return False

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
            return True

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
            log.info("event_updated", event_id=event_id, fields=list(patch.keys()))
            return True
        except Exception as exc:
            log.error("update_event_failed", title=title, error=str(exc))
            return False

    async def delete_event(self, title: str, start: Optional[datetime]) -> bool:
        service = self._get_service()
        if not service:
            return False

        match = await self._find_event(title, start)
        if not match:
            log.warning("delete_event_not_found", title=title)
            return False

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
            return True
        except Exception as exc:
            log.error("delete_event_failed", title=title, error=str(exc))
            return False


google_calendar = GoogleCalendarClient()
