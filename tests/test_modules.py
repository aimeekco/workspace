from __future__ import annotations

import unittest
from datetime import timezone

from gws_tui.modules.calendar import CalendarModule, event_day_keys, format_event_time
from gws_tui.modules.gmail import GmailModule, extract_body, format_message_date


class StubClient:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, ...], object] = {}
        self.calls: list[tuple[str, tuple[str, ...], dict | None, dict | None]] = []

    def add(self, key: tuple[str, ...], response: object) -> None:
        self.responses[key] = response

    def run(self, service: str, *segments: str, params=None, body=None, page_all=False, page_limit=5):  # noqa: ANN001
        self.calls.append((service, segments, params, body))
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
            ("calendar", "calendarList", "list", "[('maxResults', 250), ('showHidden', False)]"),
            [
                {
                    "items": [
                        {"id": "primary", "summary": "Primary", "primary": True},
                        {"id": "team", "summary": "Team"},
                    ]
                }
            ],
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
                            "maxResults": 250,
                        }.items()
                    )
                ),
            ),
            [{"items": [{"id": "1", "summary": "Standup", "start": {"dateTime": "2026-03-07T10:00:00Z"}}]}],
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
                            "maxResults": 250,
                        }.items()
                    )
                ),
            ),
            [{"items": [{"id": "2", "summary": "Retro", "start": {"dateTime": "2026-03-08T10:00:00Z"}}]}],
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].title, "Standup")
        self.assertEqual(records[1].subtitle, "Team")
        self.assertEqual(records[0].raw["day_keys"], ["2026-03-07"])

    def test_format_event_time_handles_all_day_events(self) -> None:
        self.assertEqual(format_event_time({"start": {"date": "2026-03-09"}}), "2026-03-09 all day")

    def test_build_event_body_contains_timed_range(self) -> None:
        module = CalendarModule()

        body = module.build_event_body(
            summary="Planning",
            start_text="2026-03-09 09:00",
            end_text="2026-03-09 10:00",
            location="Room 1",
            description="Agenda",
        )

        self.assertEqual(body["summary"], "Planning")
        self.assertEqual(body["location"], "Room 1")
        self.assertEqual(body["description"], "Agenda")
        self.assertIn("T09:00:00", body["start"]["dateTime"])
        self.assertIn("T10:00:00", body["end"]["dateTime"])

    def test_add_event_uses_insert_endpoint(self) -> None:
        client = StubClient()
        module = CalendarModule()
        client.add(
            ("calendar", "events", "insert", "[('calendarId', 'primary'), ('sendUpdates', 'none')]"),
            {"id": "evt-1"},
        )

        response = module.add_event(
            client,  # type: ignore[arg-type]
            calendar_id="primary",
            summary="Planning",
            start_text="2026-03-09 09:00",
            end_text="2026-03-09 10:00",
            location="Room 1",
            description="Agenda",
        )

        self.assertEqual(response["id"], "evt-1")
        self.assertEqual(client.calls[-1][1], ("events", "insert"))
        self.assertEqual(client.calls[-1][2], {"calendarId": "primary", "sendUpdates": "none"})
        self.assertEqual(client.calls[-1][3]["summary"], "Planning")

    def test_event_day_keys_spans_multi_day_all_day_event(self) -> None:
        keys = event_day_keys(
            {
                "start": {"date": "2026-03-06"},
                "end": {"date": "2026-03-09"},
            }
        )

        self.assertEqual(keys, ["2026-03-06", "2026-03-07", "2026-03-08"])


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

    def test_build_raw_message_contains_headers_and_body(self) -> None:
        module = GmailModule()

        raw = module.build_raw_message("to@example.com", "Hello", "Body text")
        raw_bytes = __import__("base64").urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        text = raw_bytes.decode("utf-8")
        self.assertIn("To: to@example.com", text)
        self.assertIn("Subject: Hello", text)
        self.assertIn("Body text", text)

    def test_send_message_uses_send_endpoint(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(("gmail", "users", "messages", "send", "[('userId', 'me')]"), {"id": "sent-1"})

        response = module.send_message(client, "to@example.com", "Hello", "Body text")  # type: ignore[arg-type]

        self.assertEqual(response["id"], "sent-1")
        self.assertEqual(client.calls[-1][0], "gmail")
        self.assertEqual(client.calls[-1][1], ("users", "messages", "send"))
        self.assertIn("raw", client.calls[-1][3])

    def test_trash_message_uses_trash_endpoint(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(("gmail", "users", "messages", "trash", "[('id', 'msg-1'), ('userId', 'me')]"), {})

        module.trash_message(client, "msg-1")  # type: ignore[arg-type]

        self.assertEqual(client.calls[-1][1], ("users", "messages", "trash"))

    def test_list_user_labels_filters_to_custom_labels(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(
            ("gmail", "users", "labels", "list", "[('userId', 'me')]"),
            {
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "SYSTEM"},
                    {"id": "Label_1", "name": "Projects", "type": "USER"},
                    {"id": "Label_2", "name": "Alpha", "type": "USER"},
                ]
            },
        )

        labels = module.list_user_labels(client)  # type: ignore[arg-type]

        self.assertEqual([label["name"] for label in labels], ["Alpha", "Projects"])

    def test_update_message_labels_uses_modify_endpoint(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(("gmail", "users", "messages", "modify", "[('id', 'msg-1'), ('userId', 'me')]"), {"id": "msg-1"})

        response = module.update_message_labels(
            client,  # type: ignore[arg-type]
            message_id="msg-1",
            existing_label_ids=["Label_1"],
            selected_label_ids=["Label_2"],
        )

        self.assertEqual(response["id"], "msg-1")
        self.assertEqual(client.calls[-1][1], ("users", "messages", "modify"))
        self.assertEqual(client.calls[-1][3], {"addLabelIds": ["Label_2"], "removeLabelIds": ["Label_1"]})


if __name__ == "__main__":
    unittest.main()
