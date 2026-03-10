from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest
from datetime import timezone

from gws_tui.models import Record
from gws_tui.modules import built_in_modules
from gws_tui.modules.calendar import CalendarModule, calendar_is_writable, event_day_keys, format_event_time, parse_duration
from gws_tui.modules.drive import DriveModule, drive_kind
from gws_tui.modules.docs import DocsModule, document_body_end_index, extract_document_text
from gws_tui.modules.gmail import GmailModule, extract_body, extract_links, format_message_date, html_to_rich_text, normalize_forward_subject, normalize_reply_subject
from gws_tui.modules.sheets import SheetsModule
from gws_tui.modules.tasks import TasksModule
from gws_tui.profiles import GwsProfile


class StubClient:
    def __init__(
        self,
        config_dir: str = "",
        responses: dict[tuple[str, ...], object] | None = None,
        calls: list[tuple[str, str, tuple[str, ...], dict | None, dict | None]] | None = None,
    ) -> None:
        self.config_dir = config_dir
        self.responses = responses if responses is not None else {}
        self.calls = calls if calls is not None else []

    def add(self, key: tuple[str, ...], response: object) -> None:
        self.responses[key] = response

    def add_for_config(self, config_dir: str, key: tuple[str, ...], response: object) -> None:
        self.responses[(config_dir, *key)] = response

    def with_config_dir(self, config_dir: str | None):  # noqa: ANN001
        return StubClient(config_dir=config_dir or "", responses=self.responses, calls=self.calls)

    def run(self, service: str, *segments: str, params=None, body=None, page_all=False, page_limit=5):  # noqa: ANN001
        self.calls.append((self.config_dir, service, segments, params, body))
        normalized = dict(params or {})
        if service == "calendar" and segments == ("events", "list"):
            if "timeMin" in normalized:
                normalized["timeMin"] = "<dynamic>"
            if "timeMax" in normalized:
                normalized["timeMax"] = "<dynamic>"
        lookup = (service, *segments, repr(sorted(normalized.items())))
        if self.config_dir:
            config_lookup = (self.config_dir, *lookup)
            if config_lookup in self.responses:
                return self.responses[config_lookup]
        return self.responses[lookup]


class BuiltInModulesTest(unittest.TestCase):
    def test_today_is_module_one(self) -> None:
        modules = built_in_modules()

        self.assertEqual([module.id for module in modules], ["today", "gmail", "calendar", "tasks", "drive", "sheets", "docs"])


class CalendarModuleTest(unittest.TestCase):
    def test_calendar_is_writable_prefers_primary_and_write_roles(self) -> None:
        self.assertTrue(calendar_is_writable({"primary": True, "accessRole": "reader"}))
        self.assertTrue(calendar_is_writable({"accessRole": "writer"}))
        self.assertTrue(calendar_is_writable({"accessRole": "owner"}))
        self.assertFalse(calendar_is_writable({"accessRole": "reader"}))

    def test_parse_duration_supports_minutes_and_hour_minute_text(self) -> None:
        self.assertEqual(parse_duration("60").total_seconds(), 3600)
        self.assertEqual(parse_duration("1h30m").total_seconds(), 5400)

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
        self.assertTrue(records[0].raw["calendar_writable"])
        self.assertFalse(records[1].raw["calendar_writable"])

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

    def test_build_event_body_supports_duration_without_end_time(self) -> None:
        module = CalendarModule()

        body = module.build_event_body(
            summary="Planning",
            start_text="2026-03-09 09:00",
            duration_text="90",
            location="Room 1",
            description="Agenda",
        )

        self.assertIn("T09:00:00", body["start"]["dateTime"])
        self.assertIn("T10:30:00", body["end"]["dateTime"])

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
        self.assertEqual(client.calls[-1][2], ("events", "insert"))
        self.assertEqual(client.calls[-1][3], {"calendarId": "primary", "sendUpdates": "none"})
        self.assertEqual(client.calls[-1][4]["summary"], "Planning")

    def test_delete_event_uses_delete_endpoint(self) -> None:
        client = StubClient()
        module = CalendarModule()
        client.add(
            ("calendar", "events", "delete", "[('calendarId', 'primary'), ('eventId', 'evt-1'), ('sendUpdates', 'none')]"),
            {},
        )

        module.delete_event(client, "primary", "evt-1")  # type: ignore[arg-type]

        self.assertEqual(client.calls[-1][2], ("events", "delete"))
        self.assertEqual(client.calls[-1][3], {"calendarId": "primary", "eventId": "evt-1", "sendUpdates": "none"})

    def test_fetch_records_merges_synced_profiles(self) -> None:
        client = StubClient(config_dir="/profiles/personal")
        module = CalendarModule()
        module.configure_profiles(
            "personal",
            [
                GwsProfile(name="personal", config_dir="/profiles/personal"),
                GwsProfile(name="school", config_dir="/profiles/school"),
            ],
            ["personal", "school"],
        )

        client.add_for_config(
            "/profiles/personal",
            ("calendar", "calendarList", "list", "[('maxResults', 250), ('showHidden', False)]"),
            [{"items": [{"id": "primary", "summary": "Primary", "primary": True}]}],
        )
        client.add_for_config(
            "/profiles/personal",
            (
                "calendar",
                "events",
                "list",
                "[('calendarId', 'primary'), ('maxResults', 250), ('orderBy', 'startTime'), ('singleEvents', True), ('timeMax', '<dynamic>'), ('timeMin', '<dynamic>')]",
            ),
            [{"items": [{"id": "evt-1", "summary": "Standup", "start": {"dateTime": "2026-03-07T10:00:00Z"}}]}],
        )
        client.add_for_config(
            "/profiles/school",
            ("calendar", "calendarList", "list", "[('maxResults', 250), ('showHidden', False)]"),
            [{"items": [{"id": "classes", "summary": "Classes"}]}],
        )
        client.add_for_config(
            "/profiles/school",
            (
                "calendar",
                "events",
                "list",
                "[('calendarId', 'classes'), ('maxResults', 250), ('orderBy', 'startTime'), ('singleEvents', True), ('timeMax', '<dynamic>'), ('timeMin', '<dynamic>')]",
            ),
            [{"items": [{"id": "evt-2", "summary": "Lecture", "start": {"dateTime": "2026-03-08T09:00:00Z"}}]}],
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].subtitle, "Primary (personal)")
        self.assertEqual(records[1].subtitle, "Classes (school)")
        self.assertEqual(records[1].raw["profile_name"], "school")
        self.assertTrue(any(call[0] == "/profiles/school" for call in client.calls))

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

    def test_extract_body_falls_back_to_html(self) -> None:
        payload = {
            "mimeType": "text/html",
            "body": {"data": "PHA+SGkgPGEgaHJlZj0iaHR0cHM6Ly9leGFtcGxlLmNvbSI+ZXhhbXBsZTwvYT48L3A+"},
        }

        self.assertEqual(extract_body(payload), "Hi example (https://example.com)")

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

    def test_build_raw_message_supports_html_body(self) -> None:
        module = GmailModule()

        raw = module.build_raw_message("to@example.com", "Hello", "<p>Body <b>text</b></p>", body_format="html")
        raw_bytes = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        text = raw_bytes.decode("utf-8")

        self.assertIn("multipart/alternative", text)
        self.assertIn("Content-Type: text/html", text)
        self.assertIn("<p>Body <b>text</b></p>", text)
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

    def test_html_to_rich_text_keeps_link_spans(self) -> None:
        rich_text = html_to_rich_text('<p>Visit <a href="https://example.com">example</a></p>')

        self.assertEqual(rich_text.plain.strip(), "Visit example")
        self.assertTrue(any("link https://example.com" in str(span.style) for span in rich_text.spans))

    def test_extract_links_deduplicates_and_trims_punctuation(self) -> None:
        links = extract_links("See https://example.com, https://example.com and https://two.example/path).")

        self.assertEqual(links, ["https://example.com", "https://two.example/path"])

    def test_send_message_uses_send_endpoint(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(("gmail", "users", "messages", "send", "[('userId', 'me')]"), {"id": "sent-1"})

        response = module.send_message(client, "to@example.com", "Hello", "Body text")  # type: ignore[arg-type]

        self.assertEqual(response["id"], "sent-1")
        self.assertEqual(client.calls[-1][1], "gmail")
        self.assertEqual(client.calls[-1][2], ("users", "messages", "send"))
        self.assertIn("raw", client.calls[-1][4])

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
        self.assertEqual(client.calls[-1][2], ("users", "messages", "send"))
        self.assertEqual(client.calls[-1][4]["threadId"], "thread-1")
        self.assertIn("raw", client.calls[-1][4])

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
        self.assertEqual(client.calls[-1][2], ("users", "drafts", "create"))
        self.assertIn("message", client.calls[-1][4])
        self.assertIn("raw", client.calls[-1][4]["message"])

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

        self.assertEqual(client.calls[-1][4]["message"]["threadId"], "thread-1")

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
        self.assertEqual(client.calls[-1][2], ("users", "messages", "list"))
        self.assertEqual(client.calls[-1][3]["q"], "from:boss@example.com is:unread")
        self.assertEqual(client.calls[-1][3]["labelIds"], "INBOX")
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
        self.assertEqual(client.calls[-1][3]["labelIds"], "SENT")
        self.assertEqual(module.list_label(), "Sent")

    def test_fetch_records_prefixes_unread_subjects(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(
            ("gmail", "users", "labels", "list", "[('userId', 'me')]"),
            {
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "SYSTEM"},
                ]
            },
        )
        client.add(
            ("gmail", "users", "messages", "list", "[('labelIds', 'INBOX'), ('maxResults', 20), ('userId', 'me')]"),
            {"messages": [{"id": "msg-1"}]},
        )
        client.add(
            ("gmail", "users", "messages", "get", "[('format', 'metadata'), ('id', 'msg-1'), ('userId', 'me')]"),
            {
                "threadId": "thread-1",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Project update"},
                        {"name": "From", "value": "A <a@example.com>"},
                        {"name": "Date", "value": "Fri, 06 Mar 2026 10:00:00 +0000"},
                    ]
                },
                "snippet": "Hello",
                "labelIds": ["INBOX", "UNREAD"],
            },
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(records[0].columns[0], "● Project update")
        self.assertTrue(records[0].raw["unread"])

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

        self.assertEqual(client.calls[-1][2], ("users", "threads", "get"))
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

        self.assertEqual(client.calls[-1][2], ("users", "messages", "trash"))

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
        self.assertEqual(client.calls[-1][2], ("users", "messages", "modify"))
        self.assertEqual(client.calls[-1][4], {"addLabelIds": ["Label_2"], "removeLabelIds": ["Label_1"]})

    def test_fetch_detail_marks_unread_message_read(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(
            ("gmail", "users", "messages", "modify", "[('id', 'msg-1'), ('userId', 'me')]"),
            {"id": "msg-1"},
        )
        client.add(
            ("gmail", "users", "messages", "get", "[('format', 'full'), ('id', 'msg-1'), ('userId', 'me')]"),
            {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Hello"},
                        {"name": "From", "value": "Alice <alice@example.com>"},
                        {"name": "To", "value": "me@example.com"},
                        {"name": "Date", "value": "Fri, 06 Mar 2026 10:00:00 +0000"},
                    ],
                    "body": {"data": "SGVsbG8"},
                },
                "snippet": "Hello",
                "labelIds": ["INBOX"],
            },
        )
        record = Record(
            key="msg-1",
            columns=("● Hello", "Alice <alice@example.com>", "Mar 06 10:00 AM"),
            title="Hello",
            subtitle="Alice <alice@example.com>",
            raw={"label_ids": ["INBOX", "UNREAD"], "unread": True},
        )

        detail = module.fetch_detail(client, record)

        self.assertIn("Subject: Hello", detail)
        self.assertEqual(client.calls[0][2], ("users", "messages", "modify"))
        self.assertFalse(record.raw["unread"])
        self.assertEqual(record.raw["label_ids"], ["INBOX"])

    def test_fetch_detail_content_marks_html_messages(self) -> None:
        client = StubClient()
        module = GmailModule()
        client.add(
            ("gmail", "users", "messages", "get", "[('format', 'full'), ('id', 'msg-1'), ('userId', 'me')]"),
            {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Hello"},
                        {"name": "From", "value": "Alice <alice@example.com>"},
                        {"name": "To", "value": "me@example.com"},
                        {"name": "Date", "value": "Fri, 06 Mar 2026 10:00:00 +0000"},
                    ],
                    "mimeType": "text/html",
                    "body": {
                        "data": "PHA+VmlzaXQgPGEgaHJlZj0iaHR0cHM6Ly9leGFtcGxlLmNvbSI+ZXhhbXBsZTwvYT48L3A+"
                    },
                },
                "snippet": "Visit example",
                "labelIds": ["INBOX"],
            },
        )
        record = Record(
            key="msg-1",
            columns=("Hello", "Alice <alice@example.com>", "Mar 06 10:00 AM"),
            title="Hello",
            subtitle="Alice <alice@example.com>",
            raw={},
        )

        detail = module.fetch_detail_content(client, record)

        self.assertIn("Content-Type: HTML", detail.text)
        self.assertIn("Visit example (https://example.com)", detail.text)
        self.assertEqual(detail.links, ["https://example.com"])


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
        self.assertEqual(client.calls[0][2], ("documents", "create"))
        self.assertEqual(client.calls[1][2], ("documents", "batchUpdate"))
        self.assertEqual(client.calls[1][4]["requests"][0]["insertText"]["text"], "Hello docs")

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
        requests = client.calls[-1][4]["requests"]
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

    def test_fetch_editor_context_reads_first_tab_values(self) -> None:
        client = StubClient()
        module = SheetsModule()
        client.add(
            ("sheets", "spreadsheets", "get", "[('includeGridData', False), ('spreadsheetId', 'sheet-1')]"),
            {
                "properties": {"title": "Budget"},
                "sheets": [{"properties": {"title": "Summary"}}],
            },
        )
        client.add(
            ("sheets", "spreadsheets", "values", "get", "[('range', \"'Summary'!A1:Z100\"), ('spreadsheetId', 'sheet-1')]"),
            {
                "values": [
                    ["Name", "Amount"],
                    ["Rent", "1200"],
                ]
            },
        )
        record = Record(
            key="sheet-1",
            columns=("Budget", "Aimee", "Mar 07 10:00 AM"),
            title="Budget",
            subtitle="Aimee",
            raw={},
        )

        context = module.fetch_editor_context(client, record)  # type: ignore[arg-type]

        self.assertEqual(context["title"], "Budget")
        self.assertEqual(context["sheet_title"], "Summary")
        self.assertEqual(context["clear_range"], "'Summary'!A1:Z100")
        self.assertEqual(context["body"], "Name | Amount\nRent | 1200")

    def test_update_sheet_values_clears_then_updates(self) -> None:
        client = StubClient()
        module = SheetsModule()
        client.add(
            ("sheets", "spreadsheets", "values", "clear", "[('range', \"'Summary'!A1:Z100\"), ('spreadsheetId', 'sheet-1')]"),
            {},
        )
        client.add(
            (
                "sheets",
                "spreadsheets",
                "values",
                "update",
                "[('range', \"'Summary'!A1:B2\"), ('spreadsheetId', 'sheet-1'), ('valueInputOption', 'USER_ENTERED')]",
            ),
            {"updatedRange": "'Summary'!A1:B2"},
        )

        module.update_sheet_values(  # type: ignore[arg-type]
            client,
            spreadsheet_id="sheet-1",
            sheet_title="Summary",
            clear_range="'Summary'!A1:Z100",
            body="Name | Amount\nRent | 1200",
        )

        self.assertEqual(client.calls[0][2], ("spreadsheets", "values", "clear"))
        self.assertEqual(client.calls[1][2], ("spreadsheets", "values", "update"))
        self.assertEqual(client.calls[1][3]["range"], "'Summary'!A1:B2")
        self.assertEqual(
            client.calls[1][4]["values"],
            [["Name", "Amount"], ["Rent", "1200"]],
        )


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


class TasksModuleTest(unittest.TestCase):
    def test_fetch_records_aggregates_tasks_across_lists(self) -> None:
        client = StubClient()
        module = TasksModule()
        client.add(
            ("tasks", "tasklists", "list", "[('maxResults', 100)]"),
            [
                {
                    "items": [
                        {"id": "list-1", "title": "Personal"},
                        {"id": "list-2", "title": "Work"},
                    ]
                }
            ],
        )
        client.add(
            (
                "tasks",
                "tasks",
                "list",
                "[('maxResults', 100), ('showAssigned', True), ('showCompleted', True), ('showDeleted', False), ('showHidden', False), ('tasklist', 'list-1')]",
            ),
            [
                {
                    "items": [
                        {
                            "id": "task-1",
                            "title": "Buy milk",
                            "status": "needsAction",
                            "due": "2026-03-09T00:00:00.000Z",
                            "updated": "2026-03-07T10:00:00.000Z",
                        }
                    ]
                }
            ],
        )
        client.add(
            (
                "tasks",
                "tasks",
                "list",
                "[('maxResults', 100), ('showAssigned', True), ('showCompleted', True), ('showDeleted', False), ('showHidden', False), ('tasklist', 'list-2')]",
            ),
            [
                {
                    "items": [
                        {
                            "id": "task-2",
                            "title": "Finish report",
                            "status": "completed",
                            "updated": "2026-03-08T10:00:00.000Z",
                        }
                    ]
                }
            ],
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].title, "Buy milk")
        self.assertEqual(records[0].subtitle, "Personal")
        self.assertEqual(records[0].raw["tasklist_id"], "list-1")
        self.assertEqual(records[1].title, "Finish report")
        self.assertEqual(records[1].subtitle, "Work")

    def test_fetch_records_respects_selected_tasklist(self) -> None:
        client = StubClient()
        module = TasksModule()
        module.set_tasklist("list-2", "Work")
        client.add(
            ("tasks", "tasklists", "list", "[('maxResults', 100)]"),
            [
                {
                    "items": [
                        {"id": "list-1", "title": "Personal"},
                        {"id": "list-2", "title": "Work"},
                    ]
                }
            ],
        )
        client.add(
            (
                "tasks",
                "tasks",
                "list",
                "[('maxResults', 100), ('showAssigned', True), ('showCompleted', True), ('showDeleted', False), ('showHidden', False), ('tasklist', 'list-2')]",
            ),
            [{"items": [{"id": "task-2", "title": "Finish report", "status": "needsAction"}]}],
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].subtitle, "Work")
        self.assertEqual(module.list_label(), "Work")

    def test_fetch_detail_reads_task(self) -> None:
        client = StubClient()
        module = TasksModule()
        client.add(
            ("tasks", "tasks", "get", "[('task', 'task-1'), ('tasklist', 'list-1')]"),
            {
                "id": "task-1",
                "title": "Buy milk",
                "status": "needsAction",
                "due": "2026-03-09T00:00:00.000Z",
                "updated": "2026-03-08T10:00:00.000Z",
                "notes": "2% only",
                "webViewLink": "https://tasks.google.com/task-1",
            },
        )

        record = Record(
            key="list-1:task-1",
            columns=("Buy milk", "Personal", "Mar 09"),
            title="Buy milk",
            subtitle="Personal",
            raw={"tasklist_id": "list-1", "task_id": "task-1", "tasklist_title": "Personal"},
        )

        detail = module.fetch_detail(client, record)

        self.assertIn("Task Overview", detail)
        self.assertIn("Task: Buy milk", detail)
        self.assertIn("List: Personal", detail)
        self.assertIn("2% only", detail)
        self.assertIn("tasks.google.com", detail)

    def test_create_task_uses_insert_endpoint(self) -> None:
        client = StubClient()
        module = TasksModule()
        client.add(("tasks", "tasks", "insert", "[('tasklist', 'list-1')]"), {"id": "task-1"})

        response = module.create_task(client, tasklist_id="list-1", title="Buy milk", notes="2% only", due_text="2026-03-09")  # type: ignore[arg-type]

        self.assertEqual(response["id"], "task-1")
        self.assertEqual(client.calls[-1][2], ("tasks", "insert"))
        self.assertEqual(client.calls[-1][4]["title"], "Buy milk")
        self.assertEqual(client.calls[-1][4]["notes"], "2% only")
        self.assertEqual(client.calls[-1][4]["due"], "2026-03-09T00:00:00Z")

    def test_update_task_status_uses_patch_endpoint(self) -> None:
        client = StubClient()
        module = TasksModule()
        client.add(("tasks", "tasks", "patch", "[('task', 'task-1'), ('tasklist', 'list-1')]"), {"id": "task-1"})
        record = Record(
            key="list-1:task-1",
            columns=("Buy milk", "Personal", "Mar 09"),
            title="Buy milk",
            subtitle="Personal",
            raw={"task": {"status": "needsAction"}, "tasklist_id": "list-1", "task_id": "task-1", "completed": False},
        )

        response = module.update_task_status(client, record, completed=True)  # type: ignore[arg-type]

        self.assertEqual(response["id"], "task-1")
        self.assertEqual(client.calls[-1][2], ("tasks", "patch"))
        self.assertEqual(client.calls[-1][4]["status"], "completed")
        self.assertIn("completed", client.calls[-1][4])
        self.assertTrue(record.raw["completed"])

    def test_fetch_records_merges_synced_profiles(self) -> None:
        client = StubClient(config_dir="/profiles/personal")
        module = TasksModule()
        module.configure_profiles(
            "school",
            [
                GwsProfile(name="personal", config_dir="/profiles/personal"),
                GwsProfile(name="school", config_dir="/profiles/school"),
            ],
            ["personal", "school"],
        )
        client.add_for_config(
            "/profiles/personal",
            ("tasks", "tasklists", "list", "[('maxResults', 100)]"),
            [{"items": [{"id": "list-1", "title": "Personal"}]}],
        )
        client.add_for_config(
            "/profiles/personal",
            (
                "tasks",
                "tasks",
                "list",
                "[('maxResults', 100), ('showAssigned', True), ('showCompleted', True), ('showDeleted', False), ('showHidden', False), ('tasklist', 'list-1')]",
            ),
            [{"items": [{"id": "task-1", "title": "Buy milk", "status": "needsAction"}]}],
        )
        client.add_for_config(
            "/profiles/school",
            ("tasks", "tasklists", "list", "[('maxResults', 100)]"),
            [{"items": [{"id": "list-2", "title": "School"}]}],
        )
        client.add_for_config(
            "/profiles/school",
            (
                "tasks",
                "tasks",
                "list",
                "[('maxResults', 100), ('showAssigned', True), ('showCompleted', True), ('showDeleted', False), ('showHidden', False), ('tasklist', 'list-2')]",
            ),
            [{"items": [{"id": "task-2", "title": "Study", "status": "needsAction"}]}],
        )

        records = module.fetch_records(client)  # type: ignore[arg-type]

        self.assertEqual(module.default_create_tasklist_id(), "school::list-2")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].subtitle, "Personal (personal)")
        self.assertEqual(records[1].subtitle, "School (school)")
        self.assertEqual(records[1].raw["profile_name"], "school")

    def test_create_task_uses_target_profile_from_synced_tasklist(self) -> None:
        client = StubClient(config_dir="/profiles/personal")
        module = TasksModule()
        module.configure_profiles(
            "personal",
            [
                GwsProfile(name="personal", config_dir="/profiles/personal"),
                GwsProfile(name="school", config_dir="/profiles/school"),
            ],
            ["personal", "school"],
        )
        client.add_for_config(
            "/profiles/school",
            ("tasks", "tasks", "insert", "[('tasklist', 'list-2')]"),
            {"id": "task-2"},
        )

        response = module.create_task(client, tasklist_id="school::list-2", title="Study")  # type: ignore[arg-type]

        self.assertEqual(response["id"], "task-2")
        self.assertEqual(client.calls[-1][0], "/profiles/school")
        self.assertEqual(client.calls[-1][2], ("tasks", "insert"))


if __name__ == "__main__":
    unittest.main()
