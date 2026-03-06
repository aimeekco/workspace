from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from html import unescape
import re

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule


def strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", unescape(value or "")).strip()


def parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def format_event_time(event: dict) -> str:
    start = event.get("start", {})
    if "dateTime" in start:
        start_time = parse_rfc3339(start["dateTime"])
        if start_time is None:
            return start["dateTime"]
        return start_time.astimezone().strftime("%b %d %I:%M %p")
    if "date" in start:
        return f'{start["date"]} all day'
    return "Unknown start"


def event_sort_key(event: dict) -> tuple[int, str]:
    start = event.get("start", {})
    raw = start.get("dateTime") or start.get("date") or "9999-12-31"
    return (0 if event.get("status") != "cancelled" else 1, raw)


class CalendarModule(WorkspaceModule):
    id = "calendar"
    title = "Calendar"
    description = "Upcoming events across your visible calendars."
    columns = ("Start", "Calendar", "Title", "Location")
    empty_message = "No upcoming events found."

    def fetch_records(self, client: GwsClient) -> list[Record]:
        calendars_response = client.run(
            "calendar",
            "calendarList",
            "list",
            params={"maxResults": 12, "showHidden": False},
        )
        calendars = calendars_response.get("items", [])
        window_start = datetime.now(UTC)
        window_end = window_start + timedelta(days=14)
        calendar_records: list[Record] = []

        ordered_calendars = sorted(
            calendars,
            key=lambda item: (not item.get("primary", False), item.get("summaryOverride") or item.get("summary") or ""),
        )

        with ThreadPoolExecutor(max_workers=4) as executor:
            batches = list(
                executor.map(
                    lambda calendar: self._fetch_calendar_records(client, calendar, window_start, window_end),
                    ordered_calendars[:6],
                )
            )

        for batch in batches:
            calendar_records.extend(batch)

        calendar_records.sort(key=lambda record: event_sort_key(record.raw["event"]))
        return calendar_records[:40]

    def _fetch_calendar_records(
        self,
        client: GwsClient,
        calendar: dict,
        window_start: datetime,
        window_end: datetime,
    ) -> list[Record]:
        calendar_id = calendar["id"]
        calendar_name = calendar.get("summaryOverride") or calendar.get("summary") or calendar_id
        events_response = client.run(
            "calendar",
            "events",
            "list",
            params={
                "calendarId": calendar_id,
                "singleEvents": True,
                "orderBy": "startTime",
                "timeMin": window_start.isoformat().replace("+00:00", "Z"),
                "timeMax": window_end.isoformat().replace("+00:00", "Z"),
                "maxResults": 10,
            },
        )
        records: list[Record] = []
        for event in events_response.get("items", []):
            if event.get("status") == "cancelled":
                continue
            summary = event.get("summary") or "(No title)"
            location = event.get("location") or ""
            preview = "\n".join(
                [
                    f"Calendar: {calendar_name}",
                    f"When: {format_event_time(event)}",
                    f"Where: {location or 'n/a'}",
                    "",
                    strip_html(event.get("description") or "Press Enter for the full event."),
                ]
            )
            records.append(
                Record(
                    key=f"{calendar_id}::{event['id']}",
                    columns=(
                        format_event_time(event),
                        calendar_name,
                        summary,
                        location,
                    ),
                    title=summary,
                    subtitle=calendar_name,
                    preview=preview,
                    raw={
                        "calendar_id": calendar_id,
                        "calendar_name": calendar_name,
                        "event": event,
                    },
                )
            )
        return records

    def fetch_detail(self, client: GwsClient, record: Record) -> str:
        event = client.run(
            "calendar",
            "events",
            "get",
            params={
                "calendarId": record.raw["calendar_id"],
                "eventId": record.raw["event"]["id"],
            },
        )
        start = format_event_time(event)
        end = format_event_time({"start": event.get("end", {})})
        attendees = event.get("attendees", [])
        attendee_lines = [attendee.get("email", "unknown attendee") for attendee in attendees[:10]]
        if len(attendees) > 10:
            attendee_lines.append(f"+{len(attendees) - 10} more")

        parts = [
            f"Title: {event.get('summary') or '(No title)'}",
            f"Calendar: {record.raw['calendar_name']}",
            f"When: {start} -> {end}",
            f"Where: {event.get('location') or 'n/a'}",
            f"Status: {event.get('status') or 'confirmed'}",
            f"Meet: {(event.get('hangoutLink') or 'n/a')}",
            "",
            "Description:",
            strip_html(event.get("description") or "n/a"),
        ]
        if attendee_lines:
            parts.extend(["", "Attendees:", *attendee_lines])
        return "\n".join(parts)
