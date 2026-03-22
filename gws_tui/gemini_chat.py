from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import json
import os
from typing import TYPE_CHECKING, Any
from urllib import error, parse, request

if TYPE_CHECKING:
    from gws_tui.planner import TodayBrief, WorkspaceContext

from gws_tui.web_search import DEFAULT_WEB_SEARCH_LIMIT, SearchResult, WebSearchService


DEFAULT_GEMINI_CHAT_MODEL = "gemini-2.0-flash"
DEFAULT_GEMINI_TIMEOUT_SECONDS = 60.0
MAX_CONTEXT_RECORDS = 40
ALLOWED_ACTION_KINDS = {"task_create", "calendar_event_create", "doc_create", "gmail_draft"}
ALLOWED_TOOL_NAMES = {"web_search"}


@dataclass(slots=True)
class GeminiChatMessage:
    role: str
    text: str
    sources: list["GeminiChatSource"] = field(default_factory=list)


@dataclass(slots=True)
class GeminiChatAction:
    kind: str
    title: str
    detail: str = ""
    module_id: str = ""
    payload: dict[str, Any] | None = None


@dataclass(slots=True)
class GeminiChatResponse:
    reply: str
    action: GeminiChatAction | None = None
    sources: list["GeminiChatSource"] = field(default_factory=list)


@dataclass(slots=True)
class GeminiChatSource:
    title: str
    url: str
    snippet: str = ""


@dataclass(slots=True)
class GeminiToolRequest:
    name: str
    query: str
    limit: int = DEFAULT_WEB_SEARCH_LIMIT


@dataclass(slots=True)
class ParsedGeminiResponse:
    reply: str
    action: GeminiChatAction | None = None
    sources: list[GeminiChatSource] = field(default_factory=list)
    tool_request: GeminiToolRequest | None = None


class GeminiChatService:
    """Gemini chat client with workspace actions and web search tool calls."""

    def __init__(
        self,
        gemini_api_key: str | None = None,
        gemini_model: str | None = None,
        timeout_seconds: float | None = None,
        web_search_service: WebSearchService | None = None,
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
            or DEFAULT_GEMINI_CHAT_MODEL
        )
        self.timeout_seconds = timeout_seconds or _env_timeout_seconds()
        self.web_search_service = web_search_service or WebSearchService()

    def respond(
        self,
        history: list[GeminiChatMessage],
        prompt: str,
        context: WorkspaceContext | None,
        brief: TodayBrief | None,
    ) -> GeminiChatResponse:
        if not self.gemini_api_key:
            raise RuntimeError("Gemini disabled: set GWS_TUI_GEMINI_API_KEY or GEMINI_API_KEY to enable chat.")

        prompt_text = self._prompt(history, prompt, context, brief)
        text = self._call_gemini(prompt_text)
        if not text:
            raise RuntimeError("Gemini returned no content.")
        parsed = self._parse_response(text)
        if parsed.tool_request is None:
            return GeminiChatResponse(reply=parsed.reply, action=parsed.action, sources=parsed.sources)

        search_results = self.web_search_service.search(parsed.tool_request.query, parsed.tool_request.limit)
        if not search_results:
            raise RuntimeError("Web search returned no results.")
        follow_up_prompt = self._search_follow_up_prompt(history, prompt, context, brief, parsed.tool_request, search_results)
        follow_up_text = self._call_gemini(follow_up_prompt)
        if not follow_up_text:
            raise RuntimeError("Gemini returned no content after web search.")
        final_response = self._parse_response(follow_up_text, available_sources=search_results)
        return GeminiChatResponse(reply=final_response.reply, action=final_response.action, sources=final_response.sources)

    def revise_after_action_error(
        self,
        history: list[GeminiChatMessage],
        failed_action: GeminiChatAction,
        error_message: str,
        context: WorkspaceContext | None,
        brief: TodayBrief | None,
    ) -> GeminiChatResponse:
        if not self.gemini_api_key:
            raise RuntimeError("Gemini disabled: set GWS_TUI_GEMINI_API_KEY or GEMINI_API_KEY to enable chat.")
        prompt_text = self._revision_prompt(history, failed_action, error_message, context, brief)
        text = self._call_gemini(prompt_text)
        if not text:
            raise RuntimeError("Gemini returned no content while revising the draft.")
        parsed = self._parse_response(text)
        return GeminiChatResponse(reply=parsed.reply, action=parsed.action, sources=parsed.sources)

    def _call_gemini(self, prompt_text: str) -> str:
        body = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "temperature": 0.4,
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
        return self._response_text(payload)

    def _prompt(
        self,
        history: list[GeminiChatMessage],
        prompt: str,
        context: WorkspaceContext | None,
        brief: TodayBrief | None,
    ) -> str:
        instructions = {
            "role": "You are a concise workspace assistant embedded in a Textual TUI.",
            "rules": [
                "Answer as plain text.",
                "Use the provided workspace context when it is relevant.",
                "Be explicit when the workspace context is incomplete or stale.",
                "Do not claim to have performed actions in Google Workspace.",
                "Keep responses compact and practical.",
                "If the user explicitly asks you to create something and you have enough detail, include one safe action proposal.",
                "If the user asks for current, recent, latest, or external web information, use the web_search tool instead of guessing.",
                "Allowed action kinds are task_create, calendar_event_create, doc_create, gmail_draft.",
                "Allowed tool requests: web_search.",
                "Do not propose destructive actions, edits, deletes, sends, or multiple actions in one response.",
                "If required fields are missing, ask a follow-up question and return action as null.",
                "When using web_search, return tool_request and leave action null.",
            ],
        }
        payload = {
            "instructions": instructions,
            "response_schema": {
                "reply": "string",
                "action": {
                    "type": "object or null",
                    "shape": {
                        "kind": "task_create|calendar_event_create|doc_create|gmail_draft",
                        "title": "string",
                        "detail": "optional string",
                        "module_id": "tasks|calendar|docs|gmail",
                        "payload": {"any": "json"},
                    },
                },
                "tool_request": {
                    "type": "object or null",
                    "shape": {
                        "name": "web_search",
                        "query": "string",
                        "limit": "integer 1-5",
                    },
                },
                "source_urls": ["string"],
            },
            "available_tools": [
                {
                    "name": "web_search",
                    "description": "Search the public web for current external information and cite URLs in the final answer.",
                    "input_shape": {"query": "string", "limit": "integer 1-5"},
                }
            ],
            "today_summary": {
                "summary": brief.summary if brief is not None else "",
                "warnings": list(brief.warnings) if brief is not None else [],
                "source": brief.source if brief is not None else "",
            },
            "workspace_context": serialize_workspace_context(context),
            "conversation_history": [{"role": message.role, "text": message.text} for message in history[-12:]],
            "user_message": prompt,
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def _search_follow_up_prompt(
        self,
        history: list[GeminiChatMessage],
        prompt: str,
        context: WorkspaceContext | None,
        brief: TodayBrief | None,
        tool_request: GeminiToolRequest,
        search_results: list[SearchResult],
    ) -> str:
        payload = {
            "instructions": {
                "role": "You are finishing a workspace assistant response after a web search.",
                "rules": [
                    "Answer as plain text.",
                    "Use the supplied web search results only for external web facts.",
                    "Cite the URLs you relied on in source_urls.",
                    "Do not request another tool call in this step.",
                    "Keep responses compact and practical.",
                ],
            },
            "response_schema": {
                "reply": "string",
                "action": {
                    "type": "object or null",
                    "shape": {
                        "kind": "task_create|calendar_event_create|doc_create|gmail_draft",
                        "title": "string",
                        "detail": "optional string",
                        "module_id": "tasks|calendar|docs|gmail",
                        "payload": {"any": "json"},
                    },
                },
                "source_urls": ["string"],
            },
            "today_summary": {
                "summary": brief.summary if brief is not None else "",
                "warnings": list(brief.warnings) if brief is not None else [],
                "source": brief.source if brief is not None else "",
            },
            "workspace_context": serialize_workspace_context(context),
            "conversation_history": [{"role": message.role, "text": message.text} for message in history[-12:]],
            "user_message": prompt,
            "tool_request": {
                "name": tool_request.name,
                "query": tool_request.query,
                "limit": tool_request.limit,
            },
            "web_search_results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                }
                for result in search_results
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def _revision_prompt(
        self,
        history: list[GeminiChatMessage],
        failed_action: GeminiChatAction,
        error_message: str,
        context: WorkspaceContext | None,
        brief: TodayBrief | None,
    ) -> str:
        payload = {
            "instructions": {
                "role": "You are revising a workspace draft after execution failed.",
                "rules": [
                    "Answer as plain text.",
                    "Use the exact error message to revise the proposed workspace action when possible.",
                    "If the error means required information is missing, ask a concise follow-up question and return action as null.",
                    "Do not use web_search in this step.",
                    "Do not propose destructive actions, edits, deletes, sends, or multiple actions.",
                    "Keep responses compact and practical.",
                ],
            },
            "response_schema": {
                "reply": "string",
                "action": {
                    "type": "object or null",
                    "shape": {
                        "kind": "task_create|calendar_event_create|doc_create|gmail_draft",
                        "title": "string",
                        "detail": "optional string",
                        "module_id": "tasks|calendar|docs|gmail",
                        "payload": {"any": "json"},
                    },
                },
                "source_urls": ["string"],
            },
            "today_summary": {
                "summary": brief.summary if brief is not None else "",
                "warnings": list(brief.warnings) if brief is not None else [],
                "source": brief.source if brief is not None else "",
            },
            "workspace_context": serialize_workspace_context(context),
            "conversation_history": [
                {
                    "role": message.role,
                    "text": message.text,
                    "sources": [{"title": source.title, "url": source.url} for source in message.sources],
                }
                for message in history[-12:]
            ],
            "failed_action": {
                "kind": failed_action.kind,
                "title": failed_action.title,
                "detail": failed_action.detail,
                "module_id": failed_action.module_id,
                "payload": failed_action.payload or {},
            },
            "error_message": error_message,
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def _response_text(self, payload: dict[str, Any]) -> str:
        parts = (
            payload.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        if not isinstance(parts, list):
            return ""
        texts = [str(part.get("text", "") or "").strip() for part in parts if isinstance(part, dict)]
        return "\n\n".join(text for text in texts if text).strip()

    def _parse_response(self, text: str, available_sources: list[SearchResult] | None = None) -> ParsedGeminiResponse:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ParsedGeminiResponse(reply=text)
        if not isinstance(payload, dict):
            return ParsedGeminiResponse(reply=text)
        reply = str(payload.get("reply", "") or "").strip() or "Done."
        action = self._parse_action(payload.get("action"))
        tool_request = self._parse_tool_request(payload.get("tool_request"))
        sources = self._parse_sources(payload.get("source_urls"), available_sources)
        return ParsedGeminiResponse(reply=reply, action=action, sources=sources, tool_request=tool_request)

    def _parse_tool_request(self, value: Any) -> GeminiToolRequest | None:
        if not isinstance(value, dict):
            return None
        name = str(value.get("name", "") or "").strip()
        query = str(value.get("query", "") or "").strip()
        if name not in ALLOWED_TOOL_NAMES or not query:
            return None
        try:
            limit = int(value.get("limit", DEFAULT_WEB_SEARCH_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_WEB_SEARCH_LIMIT
        bounded_limit = max(1, min(limit, 5))
        return GeminiToolRequest(name=name, query=query, limit=bounded_limit)

    def _parse_sources(self, value: Any, available_sources: list[SearchResult] | None) -> list[GeminiChatSource]:
        if not isinstance(value, list):
            return []
        source_map = {source.url: source for source in (available_sources or [])}
        sources: list[GeminiChatSource] = []
        seen_urls: set[str] = set()
        for item in value:
            url = str(item or "").strip()
            if not url or url in seen_urls:
                continue
            matched = source_map.get(url)
            if matched is not None:
                sources.append(GeminiChatSource(title=matched.title, url=matched.url, snippet=matched.snippet))
            else:
                sources.append(GeminiChatSource(title=url, url=url))
            seen_urls.add(url)
        return sources

    def _parse_action(self, value: Any) -> GeminiChatAction | None:
        if not isinstance(value, dict):
            return None
        kind = str(value.get("kind", "") or "").strip()
        title = str(value.get("title", "") or "").strip()
        if kind not in ALLOWED_ACTION_KINDS or not title:
            return None
        payload = value.get("payload", {})
        if not isinstance(payload, dict):
            return None
        normalized_payload = self._normalize_action_payload(kind, title, str(value.get("detail", "") or "").strip(), payload)
        if normalized_payload is None:
            return None
        normalized_title = str(normalized_payload.get("title", "") or normalized_payload.get("summary", "") or title).strip()
        return GeminiChatAction(
            kind=kind,
            title=normalized_title or title,
            detail=str(value.get("detail", "") or "").strip(),
            module_id=str(value.get("module_id", "") or "").strip(),
            payload=normalized_payload,
        )

    def _normalize_action_payload(
        self,
        kind: str,
        title: str,
        detail: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if kind == "task_create":
            task_title = str(payload.get("title", "") or title).strip()
            if not task_title:
                return None
            due_input = str(payload.get("due_text", "") or payload.get("due", "")).strip()
            due_text = _normalize_task_due_text(due_input)
            if due_input and not due_text:
                return None
            normalized: dict[str, Any] = {"title": task_title}
            notes = str(payload.get("notes", "")).strip()
            if notes:
                normalized["notes"] = notes
            if due_text:
                normalized["due_text"] = due_text
            for key in ("tasklist_id", "profile_name", "source_record_key"):
                value = str(payload.get(key, "") or "").strip()
                if value:
                    normalized[key] = value
            return normalized

        if kind == "calendar_event_create":
            summary = str(payload.get("summary", "") or title).strip()
            start_text = str(payload.get("start_text", "") or payload.get("start", "")).strip()
            if not summary or not start_text:
                return None
            normalized = {
                "summary": summary,
                "start_text": start_text,
            }
            for key, aliases in {
                "end_text": ("end_text", "end"),
                "duration_text": ("duration_text", "duration"),
                "calendar_id": ("calendar_id",),
                "location": ("location",),
                "description": ("description",),
                "profile_name": ("profile_name",),
            }.items():
                for alias in aliases:
                    value = str(payload.get(alias, "") or "").strip()
                    if value:
                        normalized[key] = value
                        break
            return normalized

        if kind == "doc_create":
            document_title = str(payload.get("title", "") or title).strip()
            if not document_title:
                return None
            normalized = {"title": document_title}
            body = str(payload.get("body", "") or detail).strip()
            if body:
                normalized["body"] = body
            return normalized

        if kind == "gmail_draft":
            to = str(payload.get("to", "")).strip()
            subject = str(payload.get("subject", "") or title).strip()
            body = str(payload.get("body", "") or detail).strip()
            if not to or not subject or not body:
                return None
            normalized = {
                "to": to,
                "subject": subject,
                "body": body,
            }
            for key in ("cc", "body_format"):
                value = str(payload.get(key, "") or "").strip()
                if value:
                    normalized[key] = value
            return normalized

        return None


def serialize_workspace_context(context: WorkspaceContext | None) -> dict[str, Any]:
    if context is None:
        return {}
    return {
        "profile_name": context.profile_name,
        "day_iso": context.day_iso,
        "default_tasklist_id": context.default_tasklist_id,
        "default_tasklist_name": context.default_tasklist_name,
        "warnings": list(context.warnings),
        "records": [
            {
                "module_id": record.module_id,
                "record_key": record.record_key,
                "title": record.title,
                "subtitle": record.subtitle,
                "timestamp": record.timestamp,
                "due_iso": record.due_iso,
                "updated_iso": record.updated_iso,
                "snippet": record.snippet,
                "url": record.url,
            }
            for record in context.records[:MAX_CONTEXT_RECORDS]
        ],
    }


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


def _normalize_task_due_text(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    for candidate in (cleaned, cleaned[:10]):
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            continue
    return ""
