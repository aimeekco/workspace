from __future__ import annotations

import unittest
from datetime import timezone

from gws_tui.modules.calendar import CalendarModule, event_day_keys, format_event_time
from gws_tui.modules.docs import DocsModule, extract_document_text
from gws_tui.modules.gmail import GmailModule, extract_body, format_message_date, normalize_reply_subject


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

    def test_normalize_reply_subject_prefixes_once(self) -> None:
        self.assertEqual(normalize_reply_subject("Hello"), "Re: Hello")
        self.assertEqual(normalize_reply_subject("Re: Hello"), "Re: Hello")

    def test_build_raw_reply_message_contains_thread_headers(self) -> None:
        module = GmailModule()

        raw = module.build_raw_reply_message(
            to="from@example.com",
            subject="Re: Hello",
            body="Reply body",
            in_reply_to="<message-id@example.com>",
            references="<prev@example.com> <message-id@example.com>",
        )
        raw_bytes = __import__("base64").urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        text = raw_bytes.decode("utf-8")
        self.assertIn("In-Reply-To: <message-id@example.com>", text)
        self.assertIn("References: <prev@example.com> <message-id@example.com>", text)
        self.assertIn("Reply body", text)

    def test_send_message_uses_send_endpoint(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(("gmail", "users", "messages", "send", "[('userId', 'me')]"), {"id": "sent-1"})

        response = module.send_message(client, "to@example.com", "Hello", "Body text")  # type: ignore[arg-type]

        self.assertEqual(response["id"], "sent-1")
        self.assertEqual(client.calls[-1][0], "gmail")
        self.assertEqual(client.calls[-1][1], ("users", "messages", "send"))
        self.assertIn("raw", client.calls[-1][3])

    def test_reply_to_message_uses_send_endpoint_with_thread_id(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(("gmail", "users", "messages", "send", "[('userId', 'me')]"), {"id": "reply-1"})

        response = module.reply_to_message(  # type: ignore[arg-type]
            client,
            to="from@example.com",
            subject="Re: Hello",
            body="Reply body",
            thread_id="thread-1",
            in_reply_to="<message-id@example.com>",
            references="<message-id@example.com>",
        )

        self.assertEqual(response["id"], "reply-1")
        self.assertEqual(client.calls[-1][1], ("users", "messages", "send"))
        self.assertEqual(client.calls[-1][3]["threadId"], "thread-1")
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


class DocsModuleTest(unittest.TestCase):
    def test_extract_document_text_collects_paragraphs(self) -> None:
        text = extract_document_text(
            {
                "body": {
                    "content": [
                        {
                            "paragraph": {
                                "elements": [
                                    {"textRun": {"content": "Hello "}},
                                    {"textRun": {"content": "world\n"}},
                                ]
                            }
                        },
                        {
                            "paragraph": {
                                "elements": [
                                    {"textRun": {"content": "Second paragraph\n"}},
                                ]
                            }
                        },
                    ]
                }
            }
        )

        self.assertEqual(text, "Hello world\n\nSecond paragraph")

    def test_fetch_records_lists_recent_docs(self) -> None:
        client = StubClient()
        module = DocsModule()
        client.add(
            (
                "drive",
                "files",
                "list",
                "[('includeItemsFromAllDrives', True), ('orderBy', 'modifiedTime desc'), ('pageSize', 25), ('q', \"mimeType='application/vnd.google-apps.document' and trashed=false\"), ('supportsAllDrives', True)]",
            ),
            [
                {
                    "files": [
                        {
                            "id": "doc-1",
                            "name": "Spec",
                            "modifiedTime": "2026-03-06T18:00:00Z",
                            "owners": [{"displayName": "Aimee"}],
                            "webViewLink": "https://docs.google.com/document/d/doc-1/edit",
                        }
                    ]
                }
            ],
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "Spec")
        self.assertEqual(records[0].subtitle, "Aimee")

    def test_fetch_detail_reads_document_text(self) -> None:
        client = StubClient()
        module = DocsModule()
        client.add(
            ("docs", "documents", "get", "[('documentId', 'doc-1'), ('includeTabsContent', False)]"),
            {
                "title": "Spec",
                "body": {
                    "content": [
                        {
                            "paragraph": {
                                "elements": [
                                    {"textRun": {"content": "Hello docs\n"}},
                                ]
                            }
                        }
                    ]
                },
            },
        )

        record = __import__("gws_tui.models", fromlist=["Record"]).Record(
            key="doc-1",
            columns=("Spec", "Aimee", "Mar 06 10:00 AM"),
            title="Spec",
            subtitle="Aimee",
            raw={"webViewLink": "https://docs.google.com/document/d/doc-1/edit"},
        )

        detail = module.fetch_detail(client, record)

        self.assertIn("Title: Spec", detail)
        self.assertIn("Hello docs", detail)


if __name__ == "__main__":
    unittest.main()
