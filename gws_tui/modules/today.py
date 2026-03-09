from __future__ import annotations

from dataclasses import dataclass, field

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule
from gws_tui.planner import (
    BriefRecordRef,
    ContextRecord,
    DraftAction,
    TodayBrief,
    TodayCache,
    TodayPlanner,
    WorkspaceContext,
    WorkspaceContextAggregator,
    brief_entry_detail,
    fallback_title,
    module_label,
    timestamp_for_context,
)


SECTION_ORDER = ("overview", "priorities", "meetings", "inbox", "documents", "drafts")
SECTION_LABELS = {
    "overview": "Overview",
    "priorities": "Top Priorities",
    "meetings": "Meetings",
    "inbox": "Inbox",
    "documents": "Documents",
    "drafts": "Draft Queue",
}


@dataclass(slots=True)
class TodayDashboard:
    summary: str
    source: str
    warnings: list[str]
    section_records: dict[str, list[Record]] = field(default_factory=dict)
    draft_actions: dict[str, DraftAction] = field(default_factory=dict)


class TodayModule(WorkspaceModule):
    id = "today"
    title = "Today"
    description = "Gemini daily briefing across your workspace."
    columns = ("Item", "Source", "When")
    empty_message = "No Today briefing available."

    def __init__(
        self,
        aggregator: WorkspaceContextAggregator | None = None,
        planner: TodayPlanner | None = None,
        cache: TodayCache | None = None,
    ) -> None:
        self.aggregator = aggregator or WorkspaceContextAggregator()
        self.planner = planner or TodayPlanner()
        self.cache = cache or TodayCache()
        self.profile_name = "default"
        self.current_context: WorkspaceContext | None = None
        self.current_brief: TodayBrief | None = None
        self.current_dashboard: TodayDashboard | None = None

    def badge(self) -> str:
        return "AI"

    def loading_message(self) -> str:
        return "Collecting workspace context and generating today's brief..."

    def empty_hint(self) -> str:
        return "Press r to load Today. Use Shift+R to ignore the cache and regenerate."

    def set_profile_name(self, profile_name: str | None) -> None:
        self.profile_name = (profile_name or "default").strip() or "default"

    def fetch_dashboard(self, client: GwsClient, force_refresh: bool = False) -> TodayDashboard:
        context = self.aggregator.collect(client, self.profile_name)
        brief = None if force_refresh else self.cache.load(self.profile_name, context.day_iso)
        if brief is None:
            brief = self.planner.generate(context)
            self.cache.save(self.profile_name, context.day_iso, brief)
        self.current_context = context
        self.current_brief = brief
        self.current_dashboard = self._build_dashboard(context, brief)
        return self.current_dashboard

    def clear_cached_brief(self) -> None:
        if self.current_context is None:
            return
        self.cache.clear(self.profile_name, self.current_context.day_iso)

    def remove_draft(self, draft_id: str) -> bool:
        if self.current_brief is None or self.current_context is None:
            return False
        remaining = [draft for draft in self.current_brief.drafts if draft.id != draft_id]
        if len(remaining) == len(self.current_brief.drafts):
            return False
        self.current_brief.drafts = remaining
        self.cache.save(self.profile_name, self.current_context.day_iso, self.current_brief)
        self.current_dashboard = self._build_dashboard(self.current_context, self.current_brief)
        return True

    def draft_by_id(self, draft_id: str) -> DraftAction | None:
        if self.current_dashboard is None:
            return None
        return self.current_dashboard.draft_actions.get(draft_id)

    def fetch_records(self, client: GwsClient) -> list[Record]:
        dashboard = self.fetch_dashboard(client)
        return dashboard.section_records.get("priorities", [])

    def fetch_detail(self, client: GwsClient, record: Record) -> str:
        return str(record.raw.get("detail", record.preview))

    def reset_state(self) -> None:
        self.current_context = None
        self.current_brief = None
        self.current_dashboard = None

    def _build_dashboard(self, context: WorkspaceContext, brief: TodayBrief) -> TodayDashboard:
        record_map = context.record_map()
        section_records: dict[str, list[Record]] = {
            "overview": [self._overview_record(context, brief)],
            "priorities": self._entry_records("priorities", brief.top_priorities, record_map, "Priority"),
            "meetings": self._entry_records("meetings", brief.meetings, record_map, "Meeting"),
            "inbox": self._entry_records("inbox", brief.important_threads, record_map, "Thread"),
            "documents": self._entry_records("documents", brief.relevant_files, record_map, "Document"),
            "drafts": self._draft_records(brief.drafts),
        }

        if not section_records["meetings"]:
            section_records["meetings"] = self._context_fallback_records("meetings", context.by_module("calendar"), "Meeting")
        if not section_records["inbox"]:
            section_records["inbox"] = self._context_fallback_records("inbox", context.by_module("gmail"), "Thread")
        if not section_records["documents"]:
            files = context.by_module("docs") + context.by_module("sheets") + context.by_module("drive")
            section_records["documents"] = self._context_fallback_records("documents", files, "Document")
        if not section_records["priorities"]:
            section_records["priorities"] = self._context_fallback_records("priorities", context.by_module("tasks"), "Task")

        return TodayDashboard(
            summary=brief.summary,
            source=brief.source,
            warnings=[*context.warnings, *brief.warnings],
            section_records=section_records,
            draft_actions={draft.id: draft for draft in brief.drafts},
        )

    def _overview_record(self, context: WorkspaceContext, brief: TodayBrief) -> Record:
        lines = [
            "Workspace Brief",
            "",
            brief.summary,
            "",
            f"Source: {brief.source}",
        ]
        if brief.schedule_risks:
            lines.extend(["", "Schedule Risks", ""])
            lines.extend(f"- {risk}" for risk in brief.schedule_risks)
        if brief.suggested_focus_blocks:
            lines.extend(["", "Suggested Focus Blocks", ""])
            for block in brief.suggested_focus_blocks:
                lines.append(f"- {block.title}: {block.detail}".strip(": "))
        warnings = [*context.warnings, *brief.warnings]
        if warnings:
            lines.extend(["", "Warnings", ""])
            lines.extend(f"- {warning}" for warning in warnings)
        return Record(
            key="today:overview",
            columns=("Workspace brief", "Today", context.day_iso),
            title="Workspace brief",
            subtitle="Today",
            preview=brief.summary,
            raw={"detail": "\n".join(lines).strip()},
        )

    def _entry_records(
        self,
        section_id: str,
        entries: list[BriefRecordRef],
        record_map: dict[str, ContextRecord],
        default_label: str,
    ) -> list[Record]:
        records: list[Record] = []
        for index, entry in enumerate(entries, start=1):
            context_record = record_map.get(entry.record_key)
            title = fallback_title(entry, context_record, f"{default_label} {index}")
            source = module_label(context_record.module_id) if context_record is not None else "Today"
            when = timestamp_for_context(context_record)
            detail = brief_entry_detail(entry, context_record) or title
            records.append(
                Record(
                    key=f"today:{section_id}:{index}:{entry.record_key or title}",
                    columns=(title, source, when),
                    title=title,
                    subtitle=source,
                    preview=entry.detail or _detail_preview(context_record, title),
                    raw={"detail": detail, "context_record_key": entry.record_key},
                )
            )
        return records

    def _context_fallback_records(self, section_id: str, items: list[ContextRecord], default_label: str) -> list[Record]:
        records: list[Record] = []
        for index, item in enumerate(items[:4], start=1):
            preview = item.snippet or item.subtitle or item.title
            lines = [f"{default_label}: {item.title}", f"Module: {module_label(item.module_id)}"]
            if item.timestamp:
                lines.append(f"When: {item.timestamp}")
            if item.due_iso:
                lines.append(f"Due: {item.due_iso}")
            if preview:
                lines.extend(["", preview])
            records.append(
                Record(
                    key=f"today:{section_id}:fallback:{index}:{item.record_key}",
                    columns=(item.title, module_label(item.module_id), item.timestamp or item.due_iso),
                    title=item.title,
                    subtitle=module_label(item.module_id),
                    preview=preview,
                    raw={"detail": "\n".join(lines).strip(), "context_record_key": item.record_key},
                )
            )
        return records

    def _draft_records(self, drafts: list[DraftAction]) -> list[Record]:
        records: list[Record] = []
        for draft in drafts:
            lines = [
                f"Draft type: {draft.kind}",
                f"Target: {module_label(draft.module_id)}" if draft.module_id else "Target: Workspace",
            ]
            if draft.detail:
                lines.extend(["", draft.detail])
            if draft.payload:
                lines.extend(["", "Payload", "", str(draft.payload)])
            records.append(
                Record(
                    key=f"today:draft:{draft.id}",
                    columns=(draft.title, module_label(draft.module_id) if draft.module_id else "Today", draft.kind),
                    title=draft.title,
                    subtitle=draft.kind,
                    preview=draft.detail or draft.kind,
                    raw={"detail": "\n".join(lines).strip(), "draft_id": draft.id},
                )
            )
        return records


def _detail_preview(record: ContextRecord | None, default: str) -> str:
    if record is None:
        return default
    if record.snippet:
        return record.snippet
    if record.subtitle:
        return record.subtitle
    return record.title
