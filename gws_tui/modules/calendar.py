from __future__ import annotations

import calendar as calendar_lib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import re
from typing import Any

from rich.console import Group
from rich.text import Text

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule
from gws_tui.profiles import GwsProfile
from gws_tui.rich_text import extract_links, html_to_rich_text, html_to_text, linkify_text

HIDDEN_PROFILE_NAMES = {"school"}


@dataclass(slots=True)
class CalendarDetail:
    text: str
    renderable: Group | Text
    links: list[str]


def strip_html(value: str) -> str:
    return html_to_text(value or "").strip()


def parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def parse_local_datetime(value: str) -> datetime:
    normalized = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(normalized, fmt).astimezone()
        except ValueError:
            continue
    raise ValueError("Use YYYY-MM-DD HH:MM")


def parse_duration(value: str) -> timedelta:
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("Duration is required when end time is blank")
    if normalized.isdigit():
        return timedelta(minutes=int(normalized))
    match = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?", normalized)
    if not match:
        raise ValueError("Use minutes like 60 or durations like 1h30m")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    total_minutes = hours * 60 + minutes
    if total_minutes <= 0:
        raise ValueError("Duration must be greater than zero")
    return timedelta(minutes=total_minutes)


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


def event_day_key(event: dict) -> str:
    start = event.get("start", {})
    if "date" in start:
        return start["date"]
    start_time = parse_rfc3339(start.get("dateTime"))
    if start_time is None:
        return ""
    return start_time.astimezone().date().isoformat()


def event_day_keys(event: dict) -> list[str]:
    start = event.get("start", {})
    end = event.get("end", {})

    if "date" in start:
        start_day = date.fromisoformat(start["date"])
        end_day = date.fromisoformat(end.get("date", start["date"])) - timedelta(days=1)
    else:
        start_time = parse_rfc3339(start.get("dateTime"))
        end_time = parse_rfc3339(end.get("dateTime"))
        if start_time is None:
            return []
        start_day = start_time.astimezone().date()
        if end_time is None:
            end_day = start_day
        else:
            # Calendar timed events use exclusive end times.
            exclusive_end = end_time.astimezone()
            end_day = (exclusive_end - timedelta(microseconds=1)).date()

    if end_day < start_day:
        end_day = start_day

    keys: list[str] = []
    current = start_day
    while current <= end_day:
        keys.append(current.isoformat())
        current += timedelta(days=1)
    return keys


def calendar_is_writable(calendar: dict[str, Any]) -> bool:
    if calendar.get("primary"):
        return True
    return calendar.get("accessRole") in {"owner", "writer"}


class CalendarModule(WorkspaceModule):
    id = "calendar"
    title = "Calendar"
    description = "Upcoming events across your visible calendars."
    columns = ("Start", "Calendar", "Title", "Location")
    empty_message = "No upcoming events found."

    def __init__(self) -> None:
        self.active_profile_name = "default"
        self.available_profiles: list[GwsProfile] = []
        self.synced_profile_names: tuple[str, ...] = ()

    def badge(self) -> str:
        return "Agenda"

    def loading_message(self) -> str:
        return "Loading this month across your visible calendars..."

    def empty_hint(self) -> str:
        return "Move with the arrow keys, use [ and ] to change month, or press a to create an event."

    def configure_profiles(
        self,
        active_profile_name: str | None,
        available_profiles: list[GwsProfile],
        synced_profile_names: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.active_profile_name = (active_profile_name or "default").strip() or "default"
        self.available_profiles = list(available_profiles)
        self.synced_profile_names = tuple(synced_profile_names or ())

    def build_event_body(
        self,
        summary: str,
        start_text: str,
        end_text: str = "",
        duration_text: str = "",
        location: str = "",
        description: str = "",
    ) -> dict:
        start = parse_local_datetime(start_text)
        if end_text.strip():
            end = parse_local_datetime(end_text)
        else:
            end = start + parse_duration(duration_text)
        if end <= start:
            raise ValueError("End must be after start")

        body = {
            "summary": summary,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        if location.strip():
            body["location"] = location.strip()
        if description.strip():
            body["description"] = description.strip()
        return body

    def add_event(
        self,
        client: GwsClient,
        calendar_id: str,
        summary: str,
        start_text: str,
        end_text: str = "",
        duration_text: str = "",
        location: str = "",
        description: str = "",
        profile_name: str | None = None,
    ) -> dict:
        target_client = self._client_for_profile(client, profile_name)
        return target_client.run(
            "calendar",
            "events",
            "insert",
            params={"calendarId": calendar_id, "sendUpdates": "none"},
            body=self.build_event_body(
                summary=summary,
                start_text=start_text,
                end_text=end_text,
                duration_text=duration_text,
                location=location,
                description=description,
            ),
        )

    def delete_event(self, client: GwsClient, calendar_id: str, event_id: str, profile_name: str | None = None) -> dict:
        target_client = self._client_for_profile(client, profile_name)
        return target_client.run(
            "calendar",
            "events",
            "delete",
            params={
                "calendarId": calendar_id,
                "eventId": event_id,
                "sendUpdates": "none",
            },
        )

    def fetch_records(self, client: GwsClient) -> list[Record]:
        today = date.today()
        return self.fetch_month_records(client, today.year, today.month)

    def fetch_month_records(self, client: GwsClient, year: int, month: int) -> list[Record]:
        target_profiles = [profile for profile in self._target_profiles() if not self._profile_hidden(profile.name)]
        if len(target_profiles) > 1:
            calendar_records: list[Record] = []
            with ThreadPoolExecutor(max_workers=min(4, len(target_profiles))) as executor:
                batches = list(
                    executor.map(
                        lambda profile: self._fetch_month_records_for_client(
                            self._client_for_profile(client, profile.name),
                            year,
                            month,
                            profile.name,
                            True,
                        ),
                        target_profiles,
                    )
                )
            for batch in batches:
                calendar_records.extend(batch)
            calendar_records.sort(key=lambda record: event_sort_key(record.raw["event"]))
            return calendar_records
        profile_name = target_profiles[0].name if len(target_profiles) == 1 else ""
        if not profile_name and self._profile_hidden(self.active_profile_name):
            return []
        return self._fetch_month_records_for_client(client, year, month, profile_name, False)

    def _fetch_month_records_for_client(
        self,
        client: GwsClient,
        year: int,
        month: int,
        profile_name: str,
        annotate_profile: bool,
    ) -> list[Record]:
        calendars_response = client.run(
            "calendar",
            "calendarList",
            "list",
            params={"maxResults": 250, "showHidden": False},
            page_all=True,
        )
        calendars = self._collect_items(calendars_response, "items")
        window_start = datetime(year, month, 1, tzinfo=UTC)
        if month == 12:
            window_end = datetime(year + 1, 1, 1, tzinfo=UTC)
        else:
            window_end = datetime(year, month + 1, 1, tzinfo=UTC)
        calendar_records: list[Record] = []

        ordered_calendars = sorted(
            calendars,
            key=lambda item: (not item.get("primary", False), item.get("summaryOverride") or item.get("summary") or ""),
        )

        with ThreadPoolExecutor(max_workers=4) as executor:
            batches = list(
                executor.map(
                    lambda calendar: self._fetch_calendar_records(
                        client,
                        calendar,
                        window_start,
                        window_end,
                        profile_name,
                        annotate_profile,
                    ),
                    ordered_calendars[:6],
                )
            )

        for batch in batches:
            calendar_records.extend(batch)

        calendar_records.sort(key=lambda record: event_sort_key(record.raw["event"]))
        return calendar_records

    def month_matrix(self, year: int, month: int) -> list[list[date]]:
        return calendar_lib.Calendar(firstweekday=6).monthdatescalendar(year, month)

    def _fetch_calendar_records(
        self,
        client: GwsClient,
        calendar: dict,
        window_start: datetime,
        window_end: datetime,
        profile_name: str = "",
        annotate_profile: bool = False,
    ) -> list[Record]:
        calendar_id = calendar["id"]
        calendar_name = calendar.get("summaryOverride") or calendar.get("summary") or calendar_id
        display_calendar_name = self._calendar_label(calendar_name, profile_name, annotate_profile)
        calendar_writable = calendar_is_writable(calendar)
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
                "maxResults": 250,
            },
            page_all=True,
        )
        records: list[Record] = []
        for event in self._collect_items(events_response, "items"):
            if event.get("status") == "cancelled":
                continue
            summary = event.get("summary") or "(No title)"
            location = event.get("location") or ""
            preview = "\n".join(
                [
                    f"Calendar: {display_calendar_name}",
                    f"When: {format_event_time(event)}",
                    f"Where: {location or 'n/a'}",
                    "",
                    strip_html(event.get("description") or "Press Enter for the full event."),
                ]
            )
            records.append(
                Record(
                    key=self._record_key(profile_name, calendar_id, event["id"], annotate_profile),
                    columns=(
                        format_event_time(event),
                        display_calendar_name,
                        summary,
                        location,
                    ),
                    title=summary,
                    subtitle=display_calendar_name,
                    preview=preview,
                    raw={
                        "day_key": event_day_key(event),
                        "day_keys": event_day_keys(event),
                        "calendar_id": calendar_id,
                        "calendar_name": calendar_name,
                        "profile_name": profile_name,
                        "calendar_writable": calendar_writable,
                        "event": event,
                    },
                )
            )
        return records

    def _collect_items(self, response: dict[str, Any] | list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        if isinstance(response, list):
            items: list[dict[str, Any]] = []
            for page in response:
                items.extend(page.get(key, []))
            return items
        return response.get(key, [])

    def fetch_detail(self, client: GwsClient, record: Record) -> str:
        return self.fetch_detail_content(client, record).text

    def fetch_detail_content(self, client: GwsClient, record: Record) -> CalendarDetail:
        detail_client = self._client_for_profile(client, record.raw.get("profile_name"))
        event = detail_client.run(
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
        description = str(event.get("description") or "").strip()
        description_text = strip_html(description or "n/a")

        parts = [
            f"Title: {event.get('summary') or '(No title)'}",
            f"Calendar: {record.raw['calendar_name']}",
            f"When: {start} -> {end}",
            f"Where: {event.get('location') or 'n/a'}",
            f"Status: {event.get('status') or 'confirmed'}",
            f"Meet: {(event.get('hangoutLink') or 'n/a')}",
            "",
            "Description:",
            description_text,
        ]
        if attendee_lines:
            parts.extend(["", "Attendees:", *attendee_lines])
        detail_text = "\n".join(parts)

        header = Text()
        header.append("Title: ", style="bold #d8dee9")
        header.append(f"{event.get('summary') or '(No title)'}")
        header.append("\n")
        header.append("Calendar: ", style="bold #d8dee9")
        header.append(f"{record.raw['calendar_name']}")
        header.append("\n")
        header.append("When: ", style="bold #d8dee9")
        header.append(f"{start} -> {end}")
        header.append("\n")
        header.append("Where: ", style="bold #d8dee9")
        header.append_text(linkify_text(event.get("location") or "n/a"))
        header.append("\n")
        header.append("Status: ", style="bold #d8dee9")
        header.append(f"{event.get('status') or 'confirmed'}")
        header.append("\n")
        header.append("Meet: ", style="bold #d8dee9")
        header.append_text(linkify_text(event.get("hangoutLink") or "n/a"))

        renderables: list[Text] = [header, Text(""), Text("Description:", style="bold #d8dee9")]
        if description:
            renderables.append(html_to_rich_text(description))
        else:
            renderables.append(Text("n/a"))
        if attendee_lines:
            attendees_text = Text()
            attendees_text.append("Attendees:\n", style="bold #d8dee9")
            attendees_text.append("\n".join(attendee_lines))
            renderables.extend([Text(""), attendees_text])

        return CalendarDetail(
            text=detail_text,
            renderable=Group(*renderables),
            links=extract_links(detail_text),
        )

    def _target_profiles(self) -> list[GwsProfile]:
        if not self.available_profiles:
            return []
        ordered: list[GwsProfile] = []
        seen_names: set[str] = set()
        if self.synced_profile_names:
            for name in self.synced_profile_names:
                profile = next((item for item in self.available_profiles if item.name == name), None)
                if profile is None or profile.name in seen_names:
                    continue
                seen_names.add(profile.name)
                ordered.append(profile)
            if ordered:
                return ordered
        active = next((item for item in self.available_profiles if item.name == self.active_profile_name), None)
        return [active] if active is not None else []

    def _client_for_profile(self, client: GwsClient, profile_name: str | None) -> GwsClient:
        if not profile_name:
            return client
        if hasattr(client, "with_config_dir"):
            profile = next((item for item in self.available_profiles if item.name == profile_name), None)
            if profile is not None:
                return client.with_config_dir(profile.config_dir)
        return client

    def _calendar_label(self, calendar_name: str, profile_name: str, annotate_profile: bool) -> str:
        if not annotate_profile or not profile_name:
            return calendar_name
        return f"{calendar_name} ({profile_name})"

    def _record_key(self, profile_name: str, calendar_id: str, event_id: str, annotate_profile: bool) -> str:
        if not annotate_profile or not profile_name:
            return f"{calendar_id}::{event_id}"
        return f"{profile_name}::{calendar_id}::{event_id}"

    def _profile_hidden(self, profile_name: str | None) -> bool:
        return (profile_name or "").strip().lower() in HIDDEN_PROFILE_NAMES
