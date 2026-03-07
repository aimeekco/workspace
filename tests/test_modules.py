from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest
from datetime import timezone

from gws_tui.models import Record
from gws_tui.modules.calendar import CalendarModule, event_day_keys, format_event_time
from gws_tui.modules.drive import DriveModule, drive_kind
from gws_tui.modules.docs import DocsModule, document_body_end_index, extract_document_text
from gws_tui.modules.gmail import GmailModule, extract_body, format_message_date, normalize_forward_subject, normalize_reply_subject
from gws_tui.modules.sheets import SheetsModule


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
        raw_bytes = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        text = raw_bytes.decode("utf-8")
        self.assertIn("To: to@example.com", text)
        self.assertIn("Subject: Hello", text)
        self.assertIn("Body text", text)

    def test_build_raw_message_contains_cc(self) -> None:
        module = GmailModule()

        raw = module.build_raw_message("to@example.com", "Hello", "Body text", cc="team@example.com")
        raw_bytes = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        text = raw_bytes.decode("utf-8")
        self.assertIn("Cc: team@example.com", text)

    def test_build_raw_message_includes_attachment(self) -> None:
        module = GmailModule()
        with tempfile.NamedTemporaryFile("w+b", suffix=".txt", delete=False) as handle:
            handle.write(b"attachment body")
            temp_path = Path(handle.name)
        self.addCleanup(temp_path.unlink)

        raw = module.build_raw_message(
            "to@example.com",
            "Hello",
            "Body text",
            attachment_paths=[str(temp_path)],
        )
        raw_bytes = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        text = raw_bytes.decode("utf-8")
        self.assertIn("multipart/mixed", text)
        self.assertIn(f'filename="{temp_path.name}"', text)

    def test_normalize_reply_subject_prefixes_once(self) -> None:
        self.assertEqual(normalize_reply_subject("Hello"), "Re: Hello")
        self.assertEqual(normalize_reply_subject("Re: Hello"), "Re: Hello")

    def test_normalize_forward_subject_prefixes_once(self) -> None:
        self.assertEqual(normalize_forward_subject("Hello"), "Fwd: Hello")
        self.assertEqual(normalize_forward_subject("Fwd: Hello"), "Fwd: Hello")

    def test_build_raw_reply_message_contains_thread_headers(self) -> None:
        module = GmailModule()

        raw = module.build_raw_reply_message(
            to="from@example.com",
            subject="Re: Hello",
            body="Reply body",
            cc="team@example.com",
            in_reply_to="<message-id@example.com>",
            references="<prev@example.com> <message-id@example.com>",
        )
        raw_bytes = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        text = raw_bytes.decode("utf-8")
        self.assertIn("In-Reply-To: <message-id@example.com>", text)
        self.assertIn("References: <prev@example.com> <message-id@example.com>", text)
        self.assertIn("Cc: team@example.com", text)
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
            cc="",
            thread_id="thread-1",
            in_reply_to="<message-id@example.com>",
            references="<message-id@example.com>",
        )

        self.assertEqual(response["id"], "reply-1")
        self.assertEqual(client.calls[-1][1], ("users", "messages", "send"))
        self.assertEqual(client.calls[-1][3]["threadId"], "thread-1")
        self.assertIn("raw", client.calls[-1][3])

    def test_create_draft_uses_drafts_endpoint(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(("gmail", "users", "drafts", "create", "[('userId', 'me')]"), {"id": "draft-1"})

        response = module.create_draft(  # type: ignore[arg-type]
            client,
            to="to@example.com",
            cc="cc@example.com",
            subject="Hello",
            body="Body text",
        )

        self.assertEqual(response["id"], "draft-1")
        self.assertEqual(client.calls[-1][1], ("users", "drafts", "create"))
        self.assertIn("message", client.calls[-1][3])
        self.assertIn("raw", client.calls[-1][3]["message"])

    def test_create_draft_sets_thread_id_for_reply_drafts(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(("gmail", "users", "drafts", "create", "[('userId', 'me')]"), {"id": "draft-2"})

        module.create_draft(  # type: ignore[arg-type]
            client,
            to="to@example.com",
            subject="Re: Hello",
            body="Body text",
            thread_id="thread-1",
            in_reply_to="<message-id@example.com>",
            references="<message-id@example.com>",
        )

        self.assertEqual(client.calls[-1][3]["message"]["threadId"], "thread-1")

    def test_mailbox_options_include_system_and_custom_labels(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(
            ("gmail", "users", "labels", "list", "[('userId', 'me')]"),
            {
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "SYSTEM"},
                    {"id": "SENT", "name": "SENT", "type": "SYSTEM"},
                    {"id": "TRASH", "name": "TRASH", "type": "SYSTEM"},
                    {"id": "Label_1", "name": "Projects", "type": "USER"},
                    {"id": "Label_2", "name": "Alpha", "type": "USER"},
                ]
            },
        )

        options = module.mailbox_options(client)  # type: ignore[arg-type]

        self.assertEqual(
            [option["name"] for option in options],
            ["Inbox", "Sent", "Trash", "Alpha", "Projects"],
        )
        self.assertEqual(module.list_label(), "Inbox")

    def test_fetch_records_uses_search_query_and_unread_filter(self) -> None:
        client = StubClient()
        module = GmailModule()
        module.set_search_query("from:boss@example.com")
        module.toggle_unread_only()
        client.add(
            ("gmail", "users", "labels", "list", "[('userId', 'me')]"),
            {
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "SYSTEM"},
                ]
            },
        )
        client.add(
            (
                "gmail",
                "users",
                "messages",
                "list",
                "[('labelIds', 'INBOX'), ('maxResults', 20), ('q', 'from:boss@example.com is:unread'), ('userId', 'me')]",
            ),
            {"messages": []},
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(records, [])
        self.assertEqual(client.calls[-1][1], ("users", "messages", "list"))
        self.assertEqual(client.calls[-1][2]["q"], "from:boss@example.com is:unread")
        self.assertEqual(client.calls[-1][2]["labelIds"], "INBOX")
        self.assertEqual(module.subtitle(), 'Mailbox: inbox, query="from:boss@example.com", unread only')

    def test_fetch_records_uses_selected_mailbox_label(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(
            ("gmail", "users", "labels", "list", "[('userId', 'me')]"),
            {
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "SYSTEM"},
                    {"id": "SENT", "name": "SENT", "type": "SYSTEM"},
                ]
            },
        )
        module.set_mailbox("SENT", "Sent")
        client.add(
            ("gmail", "users", "messages", "list", "[('labelIds', 'SENT'), ('maxResults', 20), ('userId', 'me')]"),
            {"messages": []},
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(records, [])
        self.assertEqual(client.calls[-1][2]["labelIds"], "SENT")
        self.assertEqual(module.list_label(), "Sent")

    def test_fetch_detail_reads_thread_messages(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(
            ("gmail", "users", "threads", "get", "[('format', 'full'), ('id', 'thread-1'), ('userId', 'me')]"),
            {
                "messages": [
                    {
                        "id": "msg-1",
                        "payload": {
                            "headers": [
                                {"name": "Subject", "value": "Project update"},
                                {"name": "From", "value": "A <a@example.com>"},
                                {"name": "To", "value": "me@example.com"},
                                {"name": "Date", "value": "Fri, 06 Mar 2026 10:00:00 +0000"},
                            ],
                            "body": {"data": "SGVsbG8"},
                        },
                        "snippet": "Hello",
                        "labelIds": ["INBOX"],
                    },
                    {
                        "id": "msg-2",
                        "payload": {
                            "headers": [
                                {"name": "Subject", "value": "Project update"},
                                {"name": "From", "value": "me@example.com"},
                                {"name": "To", "value": "A <a@example.com>"},
                                {"name": "Date", "value": "Fri, 06 Mar 2026 11:00:00 +0000"},
                            ],
                            "parts": [
                                {"mimeType": "text/plain", "body": {"data": "V29ybGQ"}},
                                {"filename": "notes.txt", "mimeType": "text/plain", "body": {}},
                            ],
                        },
                        "snippet": "World",
                        "labelIds": ["SENT"],
                    },
                ]
            },
        )

        record = Record(
            key="msg-2",
            columns=("A", "Project update", "Mar 06 11:00 AM"),
            title="Project update",
            subtitle="A",
            raw={"thread_id": "thread-1"},
        )

        detail = module.fetch_detail(client, record)

        self.assertEqual(client.calls[-1][1], ("users", "threads", "get"))
        self.assertIn("Thread: Project update", detail)
        self.assertIn("Messages: 2", detail)
        self.assertIn("--- Message 2 [selected] ---", detail)
        self.assertIn("Hello", detail)
        self.assertIn("World", detail)
        self.assertIn("Attachments: notes.txt", detail)
        self.assertEqual(record.raw["label_ids"], ["SENT"])

    def test_fetch_reply_all_context_excludes_current_user(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(("gmail", "users", "getProfile", "[('userId', 'me')]"), {"emailAddress": "me@example.com"})
        client.add(
            ("gmail", "users", "messages", "get", "[('format', 'full'), ('id', 'msg-1'), ('userId', 'me')]"),
            {
                "threadId": "thread-1",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Alice <alice@example.com>"},
                        {"name": "To", "value": "Me <me@example.com>, Bob <bob@example.com>"},
                        {"name": "Cc", "value": "Carol <carol@example.com>, Alice <alice@example.com>"},
                        {"name": "Subject", "value": "Hello"},
                        {"name": "Date", "value": "Fri, 06 Mar 2026 10:00:00 +0000"},
                        {"name": "Message-ID", "value": "<message-id@example.com>"},
                    ],
                    "body": {"data": "SGVsbG8"},
                },
                "snippet": "Hello",
            },
        )

        context = module.fetch_reply_all_context(client, "msg-1")  # type: ignore[arg-type]

        self.assertEqual(context["to"], "Alice <alice@example.com>")
        self.assertEqual(context["cc"], "Bob <bob@example.com>, Carol <carol@example.com>")
        self.assertEqual(context["thread_id"], "thread-1")

    def test_fetch_forward_context_builds_prefilled_body(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(
            ("gmail", "users", "messages", "get", "[('format', 'full'), ('id', 'msg-1'), ('userId', 'me')]"),
            {
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Alice <alice@example.com>"},
                        {"name": "To", "value": "me@example.com"},
                        {"name": "Cc", "value": "Bob <bob@example.com>"},
                        {"name": "Subject", "value": "Hello"},
                        {"name": "Date", "value": "Fri, 06 Mar 2026 10:00:00 +0000"},
                    ],
                    "body": {"data": "SGVsbG8"},
                },
                "snippet": "Hello",
            },
        )

        context = module.fetch_forward_context(client, "msg-1")  # type: ignore[arg-type]

        self.assertEqual(context["to"], "")
        self.assertEqual(context["cc"], "")
        self.assertEqual(context["subject"], "Fwd: Hello")
        self.assertIn("---------- Forwarded message ---------", context["body"])
        self.assertIn("From: Alice <alice@example.com>", context["body"])
        self.assertIn("Cc: Bob <bob@example.com>", context["body"])
        self.assertIn("Hello", context["body"])

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

        record = Record(
            key="doc-1",
            columns=("Spec", "Aimee", "Mar 06 10:00 AM"),
            title="Spec",
            subtitle="Aimee",
            raw={"webViewLink": "https://docs.google.com/document/d/doc-1/edit"},
        )

        detail = module.fetch_detail(client, record)

        self.assertIn("Title: Spec", detail)
        self.assertIn("Hello docs", detail)

    def test_document_body_end_index_uses_last_block(self) -> None:
        end_index = document_body_end_index(
            {
                "body": {
                    "content": [
                        {"endIndex": 1},
                        {"endIndex": 42},
                    ]
                }
            }
        )

        self.assertEqual(end_index, 42)

    def test_create_document_creates_then_inserts_text(self) -> None:
        client = StubClient()
        module = DocsModule()
        client.add(("docs", "documents", "create", "[]"), {"documentId": "doc-1", "title": "Spec"})
        client.add(("docs", "documents", "batchUpdate", "[('documentId', 'doc-1')]"), {"documentId": "doc-1"})

        response = module.create_document(client, title="Spec", body="Hello docs")  # type: ignore[arg-type]

        self.assertEqual(response["documentId"], "doc-1")
        self.assertEqual(client.calls[0][1], ("documents", "create"))
        self.assertEqual(client.calls[1][1], ("documents", "batchUpdate"))
        self.assertEqual(client.calls[1][3]["requests"][0]["insertText"]["text"], "Hello docs")

    def test_update_document_text_replaces_existing_content(self) -> None:
        client = StubClient()
        module = DocsModule()
        client.add(
            ("docs", "documents", "get", "[('documentId', 'doc-1'), ('includeTabsContent', False)]"),
            {"body": {"content": [{"endIndex": 15}]}},
        )
        client.add(("docs", "documents", "batchUpdate", "[('documentId', 'doc-1')]"), {"documentId": "doc-1"})

        response = module.update_document_text(client, "doc-1", "Updated text")  # type: ignore[arg-type]

        self.assertEqual(response["documentId"], "doc-1")
        requests = client.calls[-1][3]["requests"]
        self.assertEqual(requests[0]["deleteContentRange"]["range"], {"startIndex": 1, "endIndex": 14})
        self.assertEqual(requests[1]["insertText"]["text"], "Updated text")


class SheetsModuleTest(unittest.TestCase):
    def test_fetch_records_lists_recent_sheets(self) -> None:
        client = StubClient()
        module = SheetsModule()
        client.add(
            (
                "drive",
                "files",
                "list",
                repr(
                    sorted(
                        {
                            "q": "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                            "pageSize": 25,
                            "orderBy": "modifiedTime desc",
                            "supportsAllDrives": True,
                            "includeItemsFromAllDrives": True,
                        }.items()
                    )
                ),
            ),
            [
                {
                    "files": [
                        {
                            "id": "sheet-1",
                            "name": "Budget",
                            "modifiedTime": "2026-03-07T10:00:00Z",
                            "webViewLink": "https://docs.google.com/spreadsheets/d/sheet-1/edit",
                            "owners": [{"displayName": "Aimee"}],
                        }
                    ]
                }
            ],
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "Budget")
        self.assertEqual(records[0].subtitle, "Aimee")
        self.assertIn("docs.google.com/spreadsheets", records[0].preview)

    def test_fetch_detail_reads_spreadsheet_metadata(self) -> None:
        client = StubClient()
        module = SheetsModule()
        client.add(
            ("sheets", "spreadsheets", "get", "[('includeGridData', False), ('spreadsheetId', 'sheet-1')]"),
            {
                "properties": {
                    "title": "Budget",
                    "locale": "en_US",
                    "timeZone": "America/Los_Angeles",
                },
                "sheets": [
                    {
                        "properties": {
                            "title": "Summary",
                            "gridProperties": {
                                "rowCount": 1000,
                                "columnCount": 26,
                                "frozenRowCount": 1,
                                "frozenColumnCount": 0,
                            },
                        }
                    }
                ],
                "namedRanges": [{"name": "Totals"}],
            },
        )
        record = Record(
            key="sheet-1",
            columns=("Budget", "Aimee", "Mar 07 10:00 AM"),
            title="Budget",
            subtitle="Aimee",
            raw={"webViewLink": "https://docs.google.com/spreadsheets/d/sheet-1/edit"},
        )

        detail = module.fetch_detail(client, record)  # type: ignore[arg-type]

        self.assertIn("Spreadsheet Overview", detail)
        self.assertIn("Title: Budget", detail)
        self.assertIn("Tabs: 1", detail)
        self.assertIn("Named ranges: 1", detail)
        self.assertIn("- Summary (1000x26, frozen 1r/0c)", detail)


class DriveModuleTest(unittest.TestCase):
    def test_drive_kind_maps_common_mime_types(self) -> None:
        self.assertEqual(drive_kind({"mimeType": "application/vnd.google-apps.folder"}), "Folder")
        self.assertEqual(drive_kind({"mimeType": "application/vnd.google-apps.document"}), "Doc")
        self.assertEqual(drive_kind({"mimeType": "application/pdf"}), "PDF")

    def test_fetch_records_lists_my_drive_root_files(self) -> None:
        client = StubClient()
        module = DriveModule()
        client.add(
            (
                "drive",
                "files",
                "list",
                "[('includeItemsFromAllDrives', True), ('orderBy', 'modifiedTime desc'), ('pageSize', 25), ('q', \"'root' in parents and trashed=false\"), ('supportsAllDrives', True)]",
            ),
            [
                {
                    "files": [
                        {
                            "id": "file-1",
                            "name": "Quarterly Plan",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-03-07T18:00:00Z",
                            "owners": [{"displayName": "Aimee"}],
                            "webViewLink": "https://drive.google.com/file/d/file-1/view",
                        }
                    ]
                }
            ],
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].columns[0], "Quarterly Plan")
        self.assertEqual(records[0].columns[1], "Doc")
        self.assertEqual(records[0].subtitle, "Doc")
        self.assertEqual(module.list_label(), "My Drive")

    def test_fetch_detail_reads_drive_file_metadata(self) -> None:
        client = StubClient()
        module = DriveModule()
        client.add(
            ("drive", "files", "get", "[('fileId', 'file-1'), ('supportsAllDrives', True)]"),
            {
                "id": "file-1",
                "name": "Quarterly Plan",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-07T18:00:00Z",
                "owners": [{"displayName": "Aimee"}],
                "webViewLink": "https://drive.google.com/file/d/file-1/view",
                "size": "2048",
                "parents": ["folder-1"],
            },
        )

        record = Record(
            key="file-1",
            columns=("Quarterly Plan", "PDF", "Mar 07 10:00 AM"),
            title="Quarterly Plan",
            subtitle="PDF",
            raw={},
        )

        detail = module.fetch_detail(client, record)

        self.assertIn("Drive File Overview", detail)
        self.assertIn("Name: Quarterly Plan", detail)
        self.assertIn("Kind: PDF", detail)
        self.assertIn("Size: 2048 bytes", detail)
        self.assertIn("Parents: folder-1", detail)

    def test_fetch_records_in_folder_uses_folder_id_and_parent_row(self) -> None:
        client = StubClient()
        module = DriveModule()
        module.enter_folder(
            Record(
                key="folder-1",
                columns=("Projects", "Folder", "Mar 07 10:00 AM"),
                title="Projects",
                subtitle="Folder",
                raw={"mimeType": "application/vnd.google-apps.folder"},
            )
        )
        client.add(
            (
                "drive",
                "files",
                "list",
                "[('includeItemsFromAllDrives', True), ('orderBy', 'modifiedTime desc'), ('pageSize', 25), ('q', \"'folder-1' in parents and trashed=false\"), ('supportsAllDrives', True)]",
            ),
            [{"files": []}],
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].raw["navigate_up"])
        self.assertEqual(module.list_label(), "Projects")

    def test_navigate_up_restores_previous_folder(self) -> None:
        module = DriveModule()
        module.enter_folder(
            Record(
                key="folder-1",
                columns=("Projects", "Folder", "Mar 07 10:00 AM"),
                title="Projects",
                subtitle="Folder",
                raw={},
            )
        )
        module.navigate_up()

        self.assertEqual(module.current_folder_id, "root")
        self.assertEqual(module.list_label(), "My Drive")


if __name__ == "__main__":
    unittest.main()
