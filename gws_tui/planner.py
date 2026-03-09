from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.calendar import CalendarModule
from gws_tui.modules.docs import DocsModule
from gws_tui.modules.drive import DriveModule
from gws_tui.modules.gmail import GmailModule
from gws_tui.modules.sheets import SheetsModule
from gws_tui.modules.tasks import TasksModule


CACHE_SCHEMA_VERSION = 1
DEFAULT_CACHE_FILE = ".gws_tui_today_cache.json"
DEFAULT_GEMINI_TIMEOUT_SECONDS = 60.0
GENERIC_TASK_DRAFT_PREFIXES = ("draft ", "create ", "add ", "reminder ", "follow up")


@dataclass(slots=True)
class ContextRecord:
    module_id: str
    record_key: str
    title: str
    subtitle: str = ""
    timestamp: str = ""
    due_iso: str = ""
    updated_iso: str = ""
    snippet: str = ""
    url: str = ""


@dataclass(slots=True)
class WorkspaceContext:
    profile_name: str
    day_iso: str
    records: list[ContextRecord] = field(default_factory=list)
    default_tasklist_id: str = ""
    default_tasklist_name: str = ""
    warnings: list[str] = field(default_factory=list)

    def by_module(self, module_id: str) -> list[ContextRecord]:
        return [record for record in self.records if record.module_id == module_id]

    def record_map(self) -> dict[str, ContextRecord]:
        return {record.record_key: record for record in self.records}


@dataclass(slots=True)
class BriefRecordRef:
    record_key: str = ""
    title: str = ""
    detail: str = ""

    @classmethod
    def from_raw(cls, value: Any) -> "BriefRecordRef":
        if not isinstance(value, dict):
            return cls()
        return cls(
            record_key=str(value.get("record_key", "") or "").strip(),
            title=str(value.get("title", "") or "").strip(),
            detail=str(value.get("detail", "") or "").strip(),
        )


@dataclass(slots=True)
class FocusBlock:
    title: str
    detail: str = ""

    @classmethod
    def from_raw(cls, value: Any) -> "FocusBlock | None":
        if not isinstance(value, dict):
            return None
        title = str(value.get("title", "") or "").strip()
        if not title:
            return None
        return cls(title=title, detail=str(value.get("detail", "") or "").strip())


@dataclass(slots=True)
class DraftAction:
    id: str
    kind: str
    title: str
    detail: str = ""
    module_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, index: int, value: Any) -> "DraftAction | None":
        if not isinstance(value, dict):
            return None
        kind = str(value.get("kind", "") or "").strip()
        title = str(value.get("title", "") or "").strip()
        if not kind or not title:
            return None
        payload = value.get("payload", {})
        return cls(
            id=str(value.get("id", f"draft-{index}") or f"draft-{index}").strip(),
            kind=kind,
            title=title,
            detail=str(value.get("detail", "") or "").strip(),
            module_id=str(value.get("module_id", "") or "").strip(),
            payload=payload if isinstance(payload, dict) else {},
        )


@dataclass(slots=True)
class TodayBrief:
    summary: str
    top_priorities: list[BriefRecordRef] = field(default_factory=list)
    meetings: list[BriefRecordRef] = field(default_factory=list)
    important_threads: list[BriefRecordRef] = field(default_factory=list)
    relevant_files: list[BriefRecordRef] = field(default_factory=list)
    schedule_risks: list[str] = field(default_factory=list)
    suggested_focus_blocks: list[FocusBlock] = field(default_factory=list)
    drafts: list[DraftAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = "heuristic"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TodayBrief":
        summary = str(payload.get("summary", "") or "").strip()
        if not summary:
            summary = "No summary generated."
        return cls(
            summary=summary,
            top_priorities=[BriefRecordRef.from_raw(item) for item in payload.get("top_priorities", []) if isinstance(item, dict)],
            meetings=[BriefRecordRef.from_raw(item) for item in payload.get("meetings", []) if isinstance(item, dict)],
            important_threads=[
                BriefRecordRef.from_raw(item) for item in payload.get("important_threads", []) if isinstance(item, dict)
            ],
            relevant_files=[BriefRecordRef.from_raw(item) for item in payload.get("relevant_files", []) if isinstance(item, dict)],
            schedule_risks=[str(item).strip() for item in payload.get("schedule_risks", []) if str(item).strip()],
            suggested_focus_blocks=[
                block
                for item in payload.get("suggested_focus_blocks", [])
                if (block := FocusBlock.from_raw(item)) is not None
            ],
            drafts=[
                draft
                for index, item in enumerate(payload.get("drafts", []), start=1)
                if (draft := DraftAction.from_raw(index, item)) is not None
            ],
            warnings=[str(item).strip() for item in payload.get("warnings", []) if str(item).strip()],
            source=str(payload.get("source", "gemini") or "gemini").strip(),
        )


def _first_nonempty_line(value: str) -> str:
    for line in value.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def _tail_snippet(value: str, limit: int = 240) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        snippet = lines[0]
    else:
        snippet = " ".join(lines[1:])
    return snippet[:limit]


def _env_timeout_seconds() -> float:
    raw = (
        os.environ.get("GWS_TUI_GEMINI_TIMEOUT_SECONDS", "").strip()
        or os.environ.get("GEMINI_TIMEOUT_SECONDS", "").strip()
    )
    if not raw:
        return DEFAULT_GEMINI_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_GEMINI_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_GEMINI_TIMEOUT_SECONDS
    return value


def normalize_task_due_text(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    for candidate in (cleaned, cleaned[:10]):
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            continue
    return ""


def is_generic_task_draft_title(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    if lowered.startswith(GENERIC_TASK_DRAFT_PREFIXES):
        return True
    return "overdue task" in lowered or "draft a reminder" in lowered


def source_record_key_for_task_draft(draft: DraftAction) -> str:
    payload = draft.payload
    for key in ("source_record_key", "record_key", "task_record_key", "related_record_key"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    return ""


def task_create_defaults(draft: DraftAction, context: WorkspaceContext | None) -> tuple[str, str, str]:
    source_record: ContextRecord | None = None
    if context is not None:
        source_key = source_record_key_for_task_draft(draft)
        if source_key:
            candidate = context.record_map().get(source_key)
            if candidate is not None and candidate.module_id == "tasks":
                source_record = candidate

    explicit_title = str(draft.payload.get("title", "") or draft.title).strip()
    if source_record is not None and (not explicit_title or is_generic_task_draft_title(explicit_title)):
        title = source_record.title
    else:
        title = explicit_title or (source_record.title if source_record is not None else "")

    due_text = normalize_task_due_text(str(draft.payload.get("due", "") or draft.payload.get("due_text", "")).strip())
    if not due_text and source_record is not None and source_record.due_iso:
        source_due = normalize_task_due_text(source_record.due_iso)
        if source_due:
            due_text = (date.fromisoformat(source_due) + timedelta(days=1)).isoformat()

    notes = str(draft.payload.get("notes", "")).strip()
    return title, notes, due_text


class WorkspaceContextAggregator:
    """Collect a compact cross-module context for the Today view."""

    def collect(self, client: GwsClient, profile_name: str) -> WorkspaceContext:
        context = WorkspaceContext(profile_name=profile_name or "default", day_iso=date.today().isoformat())

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                "gmail": executor.submit(self._collect_gmail, client),
                "calendar": executor.submit(self._collect_calendar, client),
                "tasks": executor.submit(self._collect_tasks, client),
                "drive": executor.submit(self._collect_drive, client),
                "docs": executor.submit(self._collect_docs, client),
                "sheets": executor.submit(self._collect_sheets, client),
            }
            for module_id, future in futures.items():
                try:
                    records, metadata = future.result()
                except Exception as exc:  # noqa: BLE001
                    context.warnings.append(f"{module_id}: {exc}")
                    continue
                context.records.extend(records)
                if module_id == "tasks":
                    context.default_tasklist_id = metadata.get("default_tasklist_id", "")
                    context.default_tasklist_name = metadata.get("default_tasklist_name", "")

        return context

    def _collect_gmail(self, client: GwsClient) -> tuple[list[ContextRecord], dict[str, str]]:
        module = GmailModule()
        module.unread_only = True
        records = module.fetch_records(client)
        items = [
            ContextRecord(
                module_id="gmail",
                record_key=f"gmail:{record.key}",
                title=record.title,
                subtitle=record.subtitle,
                timestamp=record.columns[2] if len(record.columns) > 2 else "",
                snippet=_tail_snippet(record.preview),
            )
            for record in records[:8]
        ]
        return items, {}

    def _collect_calendar(self, client: GwsClient) -> tuple[list[ContextRecord], dict[str, str]]:
        module = CalendarModule()
        today_key = date.today().isoformat()
        records = [record for record in module.fetch_records(client) if today_key in (record.raw.get("day_keys") or [])]
        items = [
            ContextRecord(
                module_id="calendar",
                record_key=f"calendar:{record.key}",
                title=record.title,
                subtitle=record.subtitle,
                timestamp=record.columns[0] if record.columns else "",
                snippet=_tail_snippet(record.preview),
                url=str(record.raw.get("event", {}).get("htmlLink", "") or ""),
            )
            for record in records[:6]
        ]
        return items, {}

    def _collect_tasks(self, client: GwsClient) -> tuple[list[ContextRecord], dict[str, str]]:
        module = TasksModule()
        records = [record for record in module.fetch_records(client) if not bool(record.raw.get("completed"))]
        items = [
            ContextRecord(
                module_id="tasks",
                record_key=f"tasks:{record.key}",
                title=record.title,
                subtitle=record.subtitle,
                due_iso=str(record.raw.get("task", {}).get("due", "") or ""),
                updated_iso=str(record.raw.get("task", {}).get("updated", "") or ""),
                snippet=_tail_snippet(record.preview),
            )
            for record in records[:12]
        ]
        return items, {
            "default_tasklist_id": module.default_create_tasklist_id(),
            "default_tasklist_name": module.default_create_tasklist_name(),
        }

    def _collect_drive(self, client: GwsClient) -> tuple[list[ContextRecord], dict[str, str]]:
        module = DriveModule()
        records = [record for record in module.fetch_records(client) if record.key != "__drive_parent__"]
        items = [
            ContextRecord(
                module_id="drive",
                record_key=f"drive:{record.key}",
                title=record.title,
                subtitle=record.subtitle,
                timestamp=record.columns[2] if len(record.columns) > 2 else "",
                snippet=_tail_snippet(record.preview),
                url=str(record.raw.get("webViewLink", "") or ""),
            )
            for record in records[:5]
        ]
        return items, {}

    def _collect_docs(self, client: GwsClient) -> tuple[list[ContextRecord], dict[str, str]]:
        module = DocsModule()
        records = module.fetch_records(client)
        items = [
            ContextRecord(
                module_id="docs",
                record_key=f"docs:{record.key}",
                title=record.title,
                subtitle=record.subtitle,
                timestamp=record.columns[2] if len(record.columns) > 2 else "",
                snippet=_tail_snippet(record.preview),
                url=str(record.raw.get("webViewLink", "") or ""),
            )
            for record in records[:5]
        ]
        return items, {}

    def _collect_sheets(self, client: GwsClient) -> tuple[list[ContextRecord], dict[str, str]]:
        module = SheetsModule()
        records = module.fetch_records(client)
        items = [
            ContextRecord(
                module_id="sheets",
                record_key=f"sheets:{record.key}",
                title=record.title,
                subtitle=record.subtitle,
                timestamp=record.columns[2] if len(record.columns) > 2 else "",
                snippet=_tail_snippet(record.preview),
                url=str(record.raw.get("webViewLink", "") or ""),
            )
            for record in records[:5]
        ]
        return items, {}


class TodayCache:
    """Local cache for one briefing per profile and day."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self._default_path()

    def load(self, profile_name: str, day_iso: str) -> TodayBrief | None:
        payload = self._read()
        entry = payload.get("entries", {}).get(self._entry_key(profile_name, day_iso))
        if not isinstance(entry, dict):
            return None
        brief_payload = entry.get("brief")
        if not isinstance(brief_payload, dict):
            return None
        return TodayBrief.from_dict(brief_payload)

    def save(self, profile_name: str, day_iso: str, brief: TodayBrief) -> None:
        payload = self._read()
        payload.setdefault("entries", {})
        payload["entries"][self._entry_key(profile_name, day_iso)] = {
            "profile_name": profile_name,
            "day_iso": day_iso,
            "saved_at": datetime.now(UTC).isoformat(),
            "brief": brief.to_dict(),
        }
        self._write(payload)

    def clear(self, profile_name: str, day_iso: str) -> None:
        payload = self._read()
        entries = payload.get("entries", {})
        if not isinstance(entries, dict):
            return
        entries.pop(self._entry_key(profile_name, day_iso), None)
        self._write(payload)

    def _entry_key(self, profile_name: str, day_iso: str) -> str:
        return f"{profile_name}:{day_iso}"

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
        if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
            return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
        if not isinstance(payload.get("entries"), dict):
            payload["entries"] = {}
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        payload["schema_version"] = CACHE_SCHEMA_VERSION
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def _default_path(self) -> Path:
        explicit = os.environ.get("GWS_TUI_TODAY_CACHE_FILE", "").strip()
        if explicit:
            return Path(explicit).expanduser()
        return Path.cwd() / DEFAULT_CACHE_FILE


class TodayPlanner:
    """Generate a Today brief using Gemini with a deterministic fallback."""

    def __init__(
        self,
        gemini_api_key: str | None = None,
        gemini_model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.gemini_api_key = (
            gemini_api_key
            or os.environ.get("GWS_TUI_GEMINI_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
        )
        self.gemini_model = (
            gemini_model
            or os.environ.get("GWS_TUI_GEMINI_MODEL", "").strip()
            or os.environ.get("GEMINI_MODEL", "").strip()
        )
        self.timeout_seconds = timeout_seconds or _env_timeout_seconds()

    def generate(self, context: WorkspaceContext) -> TodayBrief:
        if not self.gemini_api_key:
            brief = self.fallback(context)
            brief.warnings.append("Gemini disabled: set GWS_TUI_GEMINI_API_KEY or GEMINI_API_KEY to enable AI planning.")
            return brief

        try:
            payload = self._call_gemini(context)
            brief = TodayBrief.from_dict(payload)
            brief.source = "gemini"
            return brief
        except Exception as exc:  # noqa: BLE001
            brief = self.fallback(context)
            brief.warnings.append(f"Gemini fallback: {exc}")
            return brief

    def fallback(self, context: WorkspaceContext) -> TodayBrief:
        tasks = sorted(
            context.by_module("tasks"),
            key=lambda item: (item.due_iso or "9999-12-31", item.updated_iso or "9999-12-31"),
        )
        unread = context.by_module("gmail")
        meetings = sorted(context.by_module("calendar"), key=lambda item: item.timestamp or "9999")
        files = context.by_module("docs") + context.by_module("sheets") + context.by_module("drive")

        summary_parts = [
            f"{len(tasks)} open tasks",
            f"{len(unread)} unread emails",
            f"{len(meetings)} meetings today",
        ]
        if files:
            summary_parts.append(f"{len(files[:5])} recent files worth keeping nearby")
        summary = "Today at a glance: " + ", ".join(summary_parts) + "."

        risks: list[str] = []
        if len(meetings) >= 4:
            risks.append("Calendar is crowded. Protect at least one uninterrupted focus block.")
        if any(task.due_iso for task in tasks[:3]):
            risks.append("Several top tasks have due dates. Resolve due work before reactive inbox cleanup.")
        if unread and not tasks:
            risks.append("Inbox is driving the day. Convert at least one email into explicit next actions.")

        focus_blocks = [
            FocusBlock(title="Morning focus block", detail="Start with the top due task before inbox triage."),
            FocusBlock(title="Afternoon cleanup block", detail="Clear follow-ups and prep for tomorrow."),
        ]

        return TodayBrief(
            summary=summary,
            top_priorities=[
                BriefRecordRef(record_key=item.record_key, title=item.title, detail=item.snippet or "High-priority task.")
                for item in tasks[:3]
            ],
            meetings=[
                BriefRecordRef(record_key=item.record_key, title=item.title, detail=item.snippet or item.timestamp)
                for item in meetings[:3]
            ],
            important_threads=[
                BriefRecordRef(record_key=item.record_key, title=item.title, detail=item.snippet or item.subtitle)
                for item in unread[:3]
            ],
            relevant_files=[
                BriefRecordRef(record_key=item.record_key, title=item.title, detail=item.snippet or item.subtitle)
                for item in files[:4]
            ],
            schedule_risks=risks,
            suggested_focus_blocks=focus_blocks,
            source="heuristic",
        )

    def _call_gemini(self, context: WorkspaceContext) -> dict[str, Any]:
        prompt = self._prompt(context)
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "responseMimeType": "application/json",
            },
        }
        encoded_model = parse.quote(self.gemini_model, safe="")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent?key={self.gemini_api_key}"
        http_request = request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(detail or str(exc)) from exc
        except error.URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc

        payload = json.loads(raw)
        text = (
            payload.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        if not text:
            raise RuntimeError("Gemini returned no content.")
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise RuntimeError("Gemini returned a non-object payload.")
        return parsed

    def _prompt(self, context: WorkspaceContext) -> str:
        compact_records = [
            {
                "record_key": record.record_key,
                "module_id": record.module_id,
                "title": record.title,
                "subtitle": record.subtitle,
                "timestamp": record.timestamp,
                "due_iso": record.due_iso,
                "updated_iso": record.updated_iso,
                "snippet": record.snippet,
            }
            for record in context.records
        ]
        instructions = {
            "goal": "Create a concise daily Google Workspace briefing focused on due date plus effort.",
            "constraints": [
                "Return JSON only.",
                "Prefer tasks for priorities, meetings for meetings, unread inbox for threads, docs/sheets/drive for files.",
                "Only use record_key values that exist in the supplied context.",
                "Draft kinds allowed: task_create, calendar_event_create, doc_create, gmail_draft.",
                "Draft payloads must be minimal and safe; do not propose destructive actions.",
                "For task_create drafts derived from an existing task, include payload.source_record_key and keep due/due_text in YYYY-MM-DD when present.",
                "Keep detail strings short and actionable.",
            ],
            "schema": {
                "summary": "string",
                "top_priorities": [{"record_key": "string", "title": "optional string", "detail": "string"}],
                "meetings": [{"record_key": "string", "title": "optional string", "detail": "string"}],
                "important_threads": [{"record_key": "string", "title": "optional string", "detail": "string"}],
                "relevant_files": [{"record_key": "string", "title": "optional string", "detail": "string"}],
                "schedule_risks": ["string"],
                "suggested_focus_blocks": [{"title": "string", "detail": "string"}],
                "drafts": [
                    {
                        "id": "string",
                        "kind": "task_create|calendar_event_create|doc_create|gmail_draft",
                        "title": "string",
                        "detail": "string",
                        "module_id": "tasks|calendar|docs|gmail",
                        "payload": {"any": "json"},
                    }
                ],
                "warnings": ["string"],
            },
        }
        return json.dumps(
            {
                "date": context.day_iso,
                "profile_name": context.profile_name,
                "default_tasklist_name": context.default_tasklist_name,
                "context": compact_records,
                "instructions": instructions,
            },
            indent=2,
            sort_keys=True,
        )


def module_label(module_id: str) -> str:
    labels = {
        "today": "Today",
        "gmail": "Gmail",
        "calendar": "Calendar",
        "tasks": "Tasks",
        "drive": "Drive",
        "docs": "Docs",
        "sheets": "Sheets",
    }
    return labels.get(module_id, module_id.title())


def brief_entry_detail(entry: BriefRecordRef, context_record: ContextRecord | None) -> str:
    parts: list[str] = []
    target = context_record
    if target is not None:
        parts.append(f"Module: {module_label(target.module_id)}")
        if target.subtitle:
            parts.append(f"Context: {target.subtitle}")
        if target.timestamp:
            parts.append(f"When: {target.timestamp}")
        if target.due_iso:
            parts.append(f"Due: {target.due_iso}")
        if target.snippet:
            parts.extend(["", target.snippet])
    if entry.detail:
        if parts:
            parts.extend(["", "Why it matters", "", entry.detail])
        else:
            parts.append(entry.detail)
    return "\n".join(parts).strip()


def fallback_title(entry: BriefRecordRef, context_record: ContextRecord | None, default: str) -> str:
    if entry.title:
        return entry.title
    if context_record is not None and context_record.title:
        return context_record.title
    return default


def timestamp_for_context(record: ContextRecord | None) -> str:
    if record is None:
        return ""
    return record.timestamp or record.due_iso or ""
