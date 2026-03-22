from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gws_tui.modules.today import TodayModule
from gws_tui.planner import (
    ContextRecord,
    DraftAction,
    TodayBrief,
    TodayCache,
    TodayPlanner,
    WorkspaceContext,
    task_create_defaults,
)


class FakeAggregator:
    def __init__(self, context: WorkspaceContext) -> None:
        self.context = context
        self.calls = 0

    def collect(self, client, profile_name: str):  # noqa: ANN001
        self.calls += 1
        return replace(self.context, profile_name=profile_name)


class FakePlanner:
    def __init__(self, brief: TodayBrief) -> None:
        self.brief = brief
        self.calls = 0

    def generate(self, context: WorkspaceContext) -> TodayBrief:
        self.calls += 1
        return replace(self.brief, summary=f"{self.brief.summary} ({context.profile_name})")


class TodayPlannerTest(unittest.TestCase):
    def test_fallback_prioritizes_tasks_and_unread_mail(self) -> None:
        planner = TodayPlanner(gemini_api_key="")
        context = WorkspaceContext(
            profile_name="default",
            day_iso="2026-03-09",
            records=[
                ContextRecord(
                    module_id="tasks",
                    record_key="tasks:1",
                    title="Finish report",
                    due_iso="2026-03-09T17:00:00Z",
                    updated_iso="2026-03-08T10:00:00Z",
                    snippet="Quarterly report for leadership.",
                ),
                ContextRecord(
                    module_id="tasks",
                    record_key="tasks:2",
                    title="Book travel",
                    due_iso="2026-03-10T17:00:00Z",
                    updated_iso="2026-03-08T11:00:00Z",
                    snippet="Conference next week.",
                ),
                ContextRecord(
                    module_id="gmail",
                    record_key="gmail:1",
                    title="Customer follow-up",
                    subtitle="alex@example.com",
                    snippet="Waiting on a response about the proposal.",
                ),
                ContextRecord(
                    module_id="calendar",
                    record_key="calendar:1",
                    title="Standup",
                    timestamp="Mar 09 09:30 AM",
                    snippet="Daily sync with product.",
                ),
            ],
        )

        brief = planner.fallback(context)

        self.assertEqual(brief.source, "heuristic")
        self.assertEqual(brief.top_priorities[0].record_key, "tasks:1")
        self.assertEqual(brief.important_threads[0].record_key, "gmail:1")
        self.assertIn("open tasks", brief.summary)

    def test_timeout_uses_env_override(self) -> None:
        with patch.dict(os.environ, {"GEMINI_TIMEOUT_SECONDS": "90"}, clear=False):
            planner = TodayPlanner(gemini_api_key="key")

        self.assertEqual(planner.timeout_seconds, 90.0)

    def test_task_create_defaults_use_source_task_title_and_due_plus_one_day(self) -> None:
        context = WorkspaceContext(
            profile_name="default",
            day_iso="2026-03-09",
            records=[
                ContextRecord(
                    module_id="tasks",
                    record_key="tasks:1",
                    title="[Quantum] STUDY",
                    due_iso="2026-03-07T00:00:00Z",
                    snippet="Overdue reading.",
                )
            ],
        )
        draft = DraftAction(
            id="draft-1",
            kind="task_create",
            title="Draft a reminder to complete the overdue quantum task",
            detail="Draft a reminder to complete the overdue quantum task",
            module_id="tasks",
            payload={"source_record_key": "tasks:1"},
        )

        title, notes, due_text = task_create_defaults(draft, context)

        self.assertEqual(title, "[Quantum] STUDY")
        self.assertEqual(notes, "")
        self.assertEqual(due_text, "2026-03-08")

    def test_task_create_defaults_keep_explicit_notes_and_due(self) -> None:
        context = WorkspaceContext(profile_name="default", day_iso="2026-03-09", records=[])
        draft = DraftAction(
            id="draft-1",
            kind="task_create",
            title="Follow up with advisor",
            module_id="tasks",
            payload={"title": "Follow up with advisor", "notes": "Mention budget.", "due": "2026-03-10T12:00:00Z"},
        )

        title, notes, due_text = task_create_defaults(draft, context)

        self.assertEqual(title, "Follow up with advisor")
        self.assertEqual(notes, "Mention budget.")
        self.assertEqual(due_text, "2026-03-10")


class TodayCacheTest(unittest.TestCase):
    def test_cache_round_trips_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = TodayCache(Path(tmp_dir) / ".today-cache.json")
            brief = TodayBrief(summary="Cached summary", warnings=["cached"], source="gemini")

            cache.save("work", "2026-03-09", brief)
            loaded = cache.load("work", "2026-03-09")

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.summary, "Cached summary")
            self.assertEqual(loaded.warnings, ["cached"])


class TodayModuleTest(unittest.TestCase):
    def test_fetch_dashboard_uses_cache_until_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = WorkspaceContext(
                profile_name="default",
                day_iso="2026-03-09",
                records=[
                    ContextRecord(
                        module_id="tasks",
                        record_key="tasks:1",
                        title="Write memo",
                        due_iso="2026-03-09T12:00:00Z",
                        snippet="Need this before the review.",
                    )
                ],
            )
            brief = TodayBrief(summary="Plan", source="gemini")
            aggregator = FakeAggregator(context)
            planner = FakePlanner(brief)
            cache = TodayCache(Path(tmp_dir) / ".today-cache.json")
            module = TodayModule(aggregator=aggregator, planner=planner, cache=cache)
            module.set_profile_name("work")

            module.fetch_dashboard(client=None)  # type: ignore[arg-type]
            module.fetch_dashboard(client=None)  # type: ignore[arg-type]

            self.assertEqual(planner.calls, 1)
            self.assertEqual(aggregator.calls, 2)

            module.fetch_dashboard(client=None, force_refresh=True)  # type: ignore[arg-type]

            self.assertEqual(planner.calls, 2)

    def test_fetch_dashboard_includes_overview_record_with_summary_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = WorkspaceContext(profile_name="default", day_iso="2026-03-09", records=[])
            brief = TodayBrief(
                summary="Focus on the launch checklist first.",
                source="gemini",
                warnings=["Mailbox data is partial."],
            )
            module = TodayModule(
                aggregator=FakeAggregator(context),
                planner=FakePlanner(brief),
                cache=TodayCache(Path(tmp_dir) / ".today-cache.json"),
            )

            dashboard = module.fetch_dashboard(client=None)  # type: ignore[arg-type]

            overview = dashboard.section_records["overview"][0]
            self.assertIn("Focus on the launch checklist first.", str(overview.raw["detail"]))
            self.assertIn("Mailbox data is partial.", str(overview.raw["detail"]))

    def test_remove_draft_updates_current_dashboard_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = WorkspaceContext(profile_name="default", day_iso="2026-03-09", records=[])
            brief = TodayBrief(
                summary="Plan",
                source="gemini",
                drafts=[
                    DraftAction(
                        id="draft-1",
                        kind="task_create",
                        title="Create follow-up task",
                        detail="Follow up with the vendor.",
                        module_id="tasks",
                        payload={"title": "Follow up with vendor"},
                    )
                ],
            )
            aggregator = FakeAggregator(context)
            planner = FakePlanner(brief)
            cache = TodayCache(Path(tmp_dir) / ".today-cache.json")
            module = TodayModule(aggregator=aggregator, planner=planner, cache=cache)
            module.set_profile_name("work")

            dashboard = module.fetch_dashboard(client=None)  # type: ignore[arg-type]
            self.assertEqual(len(dashboard.section_records["drafts"]), 1)

            removed = module.remove_draft("draft-1")
            refreshed = module.fetch_dashboard(client=None)  # type: ignore[arg-type]

            self.assertTrue(removed)
            self.assertEqual(len(refreshed.section_records["drafts"]), 0)
            cached = cache.load("work", "2026-03-09")
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.drafts, [])


if __name__ == "__main__":
    unittest.main()
