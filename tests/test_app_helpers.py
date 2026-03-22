from __future__ import annotations

import unittest

from gws_tui.app import Workspace, format_chat_action_preview
from gws_tui.gemini_chat import GeminiChatAction


class ChatActionPreviewTest(unittest.TestCase):
    def test_workspace_defaults_to_nord_theme(self) -> None:
        app = Workspace()

        self.assertEqual(app.theme, "nord")

    def test_calendar_preview_shows_summary_start_and_duration(self) -> None:
        preview = format_chat_action_preview(
            GeminiChatAction(
                kind="calendar_event_create",
                title="Project Sync",
                payload={
                    "summary": "Project Sync",
                    "start_text": "2026-03-24 09:00",
                    "duration_text": "45",
                    "calendar_id": "primary",
                    "location": "Zoom",
                    "description": "Review milestones.",
                },
            )
        )

        self.assertIn("Calendar Event Draft", preview)
        self.assertIn("Summary: Project Sync", preview)
        self.assertIn("Start: 2026-03-24 09:00", preview)
        self.assertIn("Duration: 45", preview)
        self.assertIn("Calendar: primary", preview)
        self.assertIn("Location: Zoom", preview)

    def test_task_preview_shows_title_due_and_notes(self) -> None:
        preview = format_chat_action_preview(
            GeminiChatAction(
                kind="task_create",
                title="Follow up",
                payload={
                    "title": "Follow up with vendor",
                    "due_text": "2026-03-25",
                    "notes": "Ask about contract timing.",
                },
            )
        )

        self.assertIn("Create Task Draft", preview)
        self.assertIn("Title: Follow up with vendor", preview)
        self.assertIn("Due: 2026-03-25", preview)
        self.assertIn("Ask about contract timing.", preview)

    def test_email_preview_shows_recipients_subject_and_body(self) -> None:
        preview = format_chat_action_preview(
            GeminiChatAction(
                kind="gmail_draft",
                title="Status update",
                payload={
                    "to": "alex@example.com",
                    "cc": "team@example.com",
                    "subject": "Status update",
                    "body": "Here is the latest status.",
                },
            )
        )

        self.assertIn("Email Draft", preview)
        self.assertIn("To: alex@example.com", preview)
        self.assertIn("Cc: team@example.com", preview)
        self.assertIn("Subject: Status update", preview)
        self.assertIn("Here is the latest status.", preview)


if __name__ == "__main__":
    unittest.main()
