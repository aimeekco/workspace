from __future__ import annotations

import unittest

from gws_tui.modules.calendar import CalendarModule, format_event_time
from gws_tui.modules.gmail import extract_body, format_message_date


class StubClient:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, ...], object] = {}

    def add(self, key: tuple[str, ...], response: object) -> None:
        self.responses[key] = response

    def run(self, service: str, *segments: str, params=None, body=None, page_all=False, page_limit=5):  # noqa: ANN001
        normalized = dict(params or {})
        if service == "calendar" and segments == ("events", "list"):
            if "timeMin" in normalized:
                normalized["timeMin"] = "<dynamic>"
            if "timeMax" in normalized:
                normalized["timeMax"] = "<dynamic>"
        lookup = (service, *segments, repr(sorted(normalized.items())))
        return self.responses[lookup]


class CalendarModuleTest(unittest.TestCase):
    def test_fetch_records_merges_upcoming_events(self) -> None:
        client = StubClient()
        module = CalendarModule()

        client.add(
            ("calendar", "calendarList", "list", "[('maxResults', 12), ('showHidden', False)]"),
            {
                "items": [
                    {"id": "primary", "summary": "Primary", "primary": True},
                    {"id": "team", "summary": "Team"},
                ]
            },
        )

        client.add(
            (
                "calendar",
                "events",
                "list",
                repr(
                    sorted(
                        {
                            "calendarId": "primary",
                            "singleEvents": True,
                            "orderBy": "startTime",
                            "timeMin": "<dynamic>",
                            "timeMax": "<dynamic>",
                            "maxResults": 10,
                        }.items()
                    )
                ),
            ),
            {"items": [{"id": "1", "summary": "Standup", "start": {"dateTime": "2026-03-07T10:00:00Z"}}]},
        )
        client.add(
            (
                "calendar",
                "events",
                "list",
                repr(
                    sorted(
                        {
                            "calendarId": "team",
                            "singleEvents": True,
                            "orderBy": "startTime",
                            "timeMin": "<dynamic>",
                            "timeMax": "<dynamic>",
                            "maxResults": 10,
                        }.items()
                    )
                ),
            ),
            {"items": [{"id": "2", "summary": "Retro", "start": {"dateTime": "2026-03-08T10:00:00Z"}}]},
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].title, "Standup")
        self.assertEqual(records[1].subtitle, "Team")

    def test_format_event_time_handles_all_day_events(self) -> None:
        self.assertEqual(format_event_time({"start": {"date": "2026-03-09"}}), "2026-03-09 all day")


class GmailHelpersTest(unittest.TestCase):
    def test_extract_body_prefers_plain_text(self) -> None:
        payload = {
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "SGVsbG8"}},
                {"mimeType": "text/html", "body": {"data": "PGRpdj5JZ25vcmU8L2Rpdj4"}},
            ]
        }

        self.assertEqual(extract_body(payload), "Hello")

    def test_format_message_date_falls_back(self) -> None:
        self.assertEqual(format_message_date(""), "Unknown date")


if __name__ == "__main__":
    unittest.main()
