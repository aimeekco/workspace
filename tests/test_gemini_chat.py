from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from gws_tui.gemini_chat import GeminiChatAction, GeminiChatMessage, GeminiChatService, serialize_workspace_context
from gws_tui.planner import ContextRecord, TodayBrief, WorkspaceContext
from gws_tui.web_search import SearchResult


class FakeWebSearchService:
    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self.results = results if results is not None else []
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        self.calls.append((query, limit))
        return self.results[:limit]


class GeminiChatServiceTest(unittest.TestCase):
    def test_reply_requires_api_key(self) -> None:
        service = GeminiChatService(gemini_api_key="", gemini_model="gemini-2.0-flash")

        with self.assertRaises(RuntimeError) as ctx:
            service.respond([], "What matters today?", None, None)

        self.assertIn("Gemini disabled", str(ctx.exception))

    def test_reply_parses_text_response(self) -> None:
        service = GeminiChatService(
            gemini_api_key="key",
            gemini_model="gemini-2.0-flash",
            timeout_seconds=10,
            web_search_service=FakeWebSearchService(),
        )
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "reply": "First paragraph.\n\nSecond paragraph.",
                                            "action": None,
                                        }
                                    )
                                },
                            ]
                        }
                    }
                ]
            }
        ).encode("utf-8")
        urlopen_result = MagicMock()
        urlopen_result.__enter__.return_value = response
        urlopen_result.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=urlopen_result):
            payload = service.respond(
                [GeminiChatMessage(role="assistant", text="You have one overdue task.")],
                "What should I do first?",
                WorkspaceContext(profile_name="work", day_iso="2026-03-21"),
                TodayBrief(summary="One task and two meetings.", source="gemini"),
            )

        self.assertEqual(payload.reply, "First paragraph.\n\nSecond paragraph.")
        self.assertIsNone(payload.action)
        self.assertEqual(payload.sources, [])

    def test_respond_runs_web_search_tool_and_returns_sources(self) -> None:
        search_service = FakeWebSearchService(
            results=[
                SearchResult(
                    title="OpenAI release notes",
                    url="https://example.com/release-notes",
                    snippet="Latest release details.",
                ),
                SearchResult(
                    title="Model guide",
                    url="https://example.com/model-guide",
                    snippet="Version summary.",
                ),
            ]
        )
        service = GeminiChatService(
            gemini_api_key="key",
            gemini_model="gemini-2.0-flash",
            timeout_seconds=10,
            web_search_service=search_service,
        )
        responses = iter(
            [
                json.dumps(
                    {
                        "reply": "Let me check the web.",
                        "tool_request": {"name": "web_search", "query": "latest Gemini model", "limit": 2},
                        "action": None,
                        "source_urls": [],
                    }
                ),
                json.dumps(
                    {
                        "reply": "The latest updates are in the release notes.",
                        "action": None,
                        "source_urls": ["https://example.com/release-notes"],
                    }
                ),
            ]
        )

        with patch.object(service, "_call_gemini", side_effect=lambda prompt: next(responses)) as call_mock:
            payload = service.respond([], "What's the latest Gemini model?", None, None)

        self.assertEqual(call_mock.call_count, 2)
        self.assertEqual(search_service.calls, [("latest Gemini model", 2)])
        self.assertEqual(payload.reply, "The latest updates are in the release notes.")
        self.assertEqual(len(payload.sources), 1)
        self.assertEqual(payload.sources[0].url, "https://example.com/release-notes")

    def test_prompt_includes_summary_context_and_history(self) -> None:
        service = GeminiChatService(gemini_api_key="key", gemini_model="gemini-2.0-flash", web_search_service=FakeWebSearchService())
        context = WorkspaceContext(
            profile_name="work",
            day_iso="2026-03-21",
            records=[
                ContextRecord(
                    module_id="tasks",
                    record_key="tasks:1",
                    title="Finish report",
                    due_iso="2026-03-21",
                    snippet="Needs to go out today.",
                )
            ],
            warnings=["Tasks may be stale."],
        )
        brief = TodayBrief(summary="Finish the report before noon.", warnings=["Gemini fallback: timeout"], source="heuristic")

        prompt = service._prompt(
            [GeminiChatMessage(role="user", text="What should I prioritize?")],
            "Draft a plan for the afternoon.",
            context,
            brief,
        )

        self.assertIn("Finish the report before noon.", prompt)
        self.assertIn("tasks:1", prompt)
        self.assertIn("Draft a plan for the afternoon.", prompt)
        self.assertIn("What should I prioritize?", prompt)
        self.assertIn("task_create", prompt)
        self.assertIn("web_search", prompt)

    def test_search_follow_up_prompt_includes_results(self) -> None:
        service = GeminiChatService(gemini_api_key="key", gemini_model="gemini-2.0-flash", web_search_service=FakeWebSearchService())

        prompt = service._search_follow_up_prompt(
            history=[],
            prompt="What changed recently?",
            context=None,
            brief=None,
            tool_request=service._parse_tool_request({"name": "web_search", "query": "latest release", "limit": 1}),  # type: ignore[arg-type]
            search_results=[SearchResult(title="Release notes", url="https://example.com/release", snippet="Update summary.")],
        )

        self.assertIn("latest release", prompt)
        self.assertIn("https://example.com/release", prompt)
        self.assertIn("source_urls", prompt)

    def test_revise_after_action_error_returns_revised_action(self) -> None:
        service = GeminiChatService(gemini_api_key="key", gemini_model="gemini-2.0-flash", web_search_service=FakeWebSearchService())

        with patch.object(
            service,
            "_call_gemini",
            return_value=json.dumps(
                {
                    "reply": "I updated the event to use a start time.",
                    "action": {
                        "kind": "calendar_event_create",
                        "title": "Project sync",
                        "payload": {
                            "start_text": "2026-03-25 10:00",
                            "duration_text": "30",
                        },
                    },
                }
            ),
        ):
            response = service.revise_after_action_error(
                history=[GeminiChatMessage(role="user", text="Make a project sync event.")],
                failed_action=GeminiChatAction(kind="calendar_event_create", title="Project sync", payload={"duration_text": "30"}),
                error_message="Calendar action requires summary and start time",
                context=None,
                brief=None,
            )

        self.assertEqual(response.reply, "I updated the event to use a start time.")
        self.assertIsNotNone(response.action)
        assert response.action is not None
        self.assertEqual(response.action.payload["start_text"], "2026-03-25 10:00")

    def test_revision_prompt_includes_failed_action_and_error(self) -> None:
        service = GeminiChatService(gemini_api_key="key", gemini_model="gemini-2.0-flash", web_search_service=FakeWebSearchService())

        prompt = service._revision_prompt(
            history=[GeminiChatMessage(role="user", text="Make a reminder task.")],
            failed_action=GeminiChatAction(kind="task_create", title="Reminder", payload={"title": "Reminder", "due_text": "2026-03-25"}),
            error_message="No writable task list available",
            context=None,
            brief=None,
        )

        self.assertIn("No writable task list available", prompt)
        self.assertIn('"failed_action"', prompt)
        self.assertIn('"due_text": "2026-03-25"', prompt)

    def test_parse_response_keeps_allowed_action(self) -> None:
        service = GeminiChatService(gemini_api_key="key", gemini_model="gemini-2.0-flash", web_search_service=FakeWebSearchService())

        response = service._parse_response(
            json.dumps(
                {
                    "reply": "I can create that follow-up task.",
                    "action": {
                        "kind": "task_create",
                        "title": "Follow up with vendor",
                        "detail": "Mention contract timing.",
                        "module_id": "tasks",
                        "payload": {"due": "2026-03-22T12:00:00Z"},
                    },
                }
            )
        )

        self.assertEqual(response.reply, "I can create that follow-up task.")
        self.assertIsNotNone(response.action)
        assert response.action is not None
        self.assertEqual(response.action.kind, "task_create")
        self.assertEqual(response.action.payload["title"], "Follow up with vendor")
        self.assertEqual(response.action.payload["due_text"], "2026-03-22")

    def test_parse_response_discards_task_action_with_invalid_due_format(self) -> None:
        service = GeminiChatService(gemini_api_key="key", gemini_model="gemini-2.0-flash", web_search_service=FakeWebSearchService())

        response = service._parse_response(
            json.dumps(
                {
                    "reply": "I need an exact due date first.",
                    "action": {
                        "kind": "task_create",
                        "title": "Follow up with vendor",
                        "payload": {"due_text": "tomorrow"},
                    },
                }
            )
        )

        self.assertEqual(response.reply, "I need an exact due date first.")
        self.assertIsNone(response.action)

    def test_parse_response_normalizes_event_action_aliases(self) -> None:
        service = GeminiChatService(gemini_api_key="key", gemini_model="gemini-2.0-flash", web_search_service=FakeWebSearchService())

        response = service._parse_response(
            json.dumps(
                {
                    "reply": "I can draft that event.",
                    "action": {
                        "kind": "calendar_event_create",
                        "title": "Project sync",
                        "payload": {
                            "start": "2026-03-22 09:00",
                            "duration": "30",
                        },
                    },
                }
            )
        )

        self.assertIsNotNone(response.action)
        assert response.action is not None
        self.assertEqual(response.action.payload["summary"], "Project sync")
        self.assertEqual(response.action.payload["start_text"], "2026-03-22 09:00")
        self.assertEqual(response.action.payload["duration_text"], "30")

    def test_parse_response_discards_unsupported_action(self) -> None:
        service = GeminiChatService(gemini_api_key="key", gemini_model="gemini-2.0-flash", web_search_service=FakeWebSearchService())

        response = service._parse_response(
            json.dumps(
                {
                    "reply": "I won't delete anything.",
                    "action": {
                        "kind": "calendar_event_delete",
                        "title": "Delete event",
                    },
                }
            )
        )

        self.assertEqual(response.reply, "I won't delete anything.")
        self.assertIsNone(response.action)

    def test_parse_response_keeps_web_search_request(self) -> None:
        service = GeminiChatService(gemini_api_key="key", gemini_model="gemini-2.0-flash", web_search_service=FakeWebSearchService())

        response = service._parse_response(
            json.dumps(
                {
                    "reply": "I'll look that up.",
                    "tool_request": {
                        "name": "web_search",
                        "query": "latest Gemini model",
                        "limit": 7,
                    },
                    "action": None,
                }
            )
        )

        self.assertIsNotNone(response.tool_request)
        assert response.tool_request is not None
        self.assertEqual(response.tool_request.name, "web_search")
        self.assertEqual(response.tool_request.query, "latest Gemini model")
        self.assertEqual(response.tool_request.limit, 5)


class SerializeWorkspaceContextTest(unittest.TestCase):
    def test_serialize_workspace_context_returns_expected_fields(self) -> None:
        context = WorkspaceContext(
            profile_name="work",
            day_iso="2026-03-21",
            default_tasklist_id="list-1",
            default_tasklist_name="My Tasks",
            warnings=["warning"],
            records=[ContextRecord(module_id="gmail", record_key="gmail:1", title="Unread message", subtitle="alex@example.com")],
        )

        payload = serialize_workspace_context(context)

        self.assertEqual(payload["profile_name"], "work")
        self.assertEqual(payload["default_tasklist_name"], "My Tasks")
        self.assertEqual(payload["records"][0]["record_key"], "gmail:1")


if __name__ == "__main__":
    unittest.main()
