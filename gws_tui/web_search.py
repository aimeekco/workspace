from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import json
import os
import re
from typing import Any
from urllib import parse, request


DEFAULT_WEB_SEARCH_PROVIDER = "duckduckgo"
DEFAULT_WEB_SEARCH_TIMEOUT_SECONDS = 20.0
DEFAULT_WEB_SEARCH_LIMIT = 5

_RESULT_LINK_RE = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>(?P<tail>.*?)(?=<a[^>]*class="[^"]*result__a|$)',
    re.IGNORECASE | re.DOTALL,
)
_RESULT_SNIPPET_RE = re.compile(
    r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>|<div[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(?P<div_snippet>.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class WebSearchService:
    """Provider-backed web search service for Gemini tool calls."""

    def __init__(
        self,
        provider: str | None = None,
        brave_api_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.provider = (
            provider
            or os.environ.get("GWS_TUI_WEB_SEARCH_PROVIDER", "").strip()
            or os.environ.get("WEB_SEARCH_PROVIDER", "").strip()
            or DEFAULT_WEB_SEARCH_PROVIDER
        ).lower()
        self.brave_api_key = (
            brave_api_key
            or os.environ.get("GWS_TUI_BRAVE_SEARCH_API_KEY", "").strip()
            or os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        )
        self.timeout_seconds = timeout_seconds or _env_timeout_seconds()

    def search(self, query: str, limit: int = DEFAULT_WEB_SEARCH_LIMIT) -> list[SearchResult]:
        cleaned_query = query.strip()
        if not cleaned_query:
            raise RuntimeError("Web search query is required.")
        bounded_limit = max(1, min(limit, 5))
        if self.provider == "duckduckgo":
            return self._search_duckduckgo(cleaned_query, bounded_limit)
        if self.provider == "brave":
            return self._search_brave(cleaned_query, bounded_limit)
        raise RuntimeError(f"Unsupported web search provider: {self.provider}")

    def _search_duckduckgo(self, query: str, limit: int) -> list[SearchResult]:
        url = "https://html.duckduckgo.com/html/?q=" + parse.quote_plus(query)
        http_request = request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; gws-tui/0.1; +https://example.invalid)",
            },
            method="GET",
        )
        with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
            html = response.read().decode("utf-8", errors="replace")
        results = parse_duckduckgo_html(html)
        return results[:limit]

    def _search_brave(self, query: str, limit: int) -> list[SearchResult]:
        if not self.brave_api_key:
            raise RuntimeError("Brave web search requires GWS_TUI_BRAVE_SEARCH_API_KEY or BRAVE_SEARCH_API_KEY.")
        url = "https://api.search.brave.com/res/v1/web/search?" + parse.urlencode({"q": query, "count": limit})
        http_request = request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.brave_api_key,
            },
            method="GET",
        )
        with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return parse_brave_search_payload(payload)[:limit]


def parse_duckduckgo_html(html: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for match in _RESULT_LINK_RE.finditer(html):
        url = _normalize_duckduckgo_url(match.group("href"))
        title = _clean_html_text(match.group("title"))
        if not url or not title or url in seen_urls:
            continue
        tail = match.group("tail")
        snippet_match = _RESULT_SNIPPET_RE.search(tail)
        snippet = ""
        if snippet_match is not None:
            snippet = _clean_html_text(snippet_match.group("snippet") or snippet_match.group("div_snippet") or "")
        results.append(SearchResult(title=title, url=url, snippet=snippet))
        seen_urls.add(url)
    return results


def parse_brave_search_payload(payload: dict[str, Any]) -> list[SearchResult]:
    web = payload.get("web", {})
    results: list[SearchResult] = []
    if not isinstance(web, dict):
        return results
    for item in web.get("results", []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or "").strip()
        url = str(item.get("url", "") or "").strip()
        snippet = str(item.get("description", "") or "").strip()
        if title and url:
            results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results


def _normalize_duckduckgo_url(href: str) -> str:
    cleaned = unescape(href).strip()
    if not cleaned:
        return ""
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    parsed = parse.urlparse(cleaned)
    if "duckduckgo.com" in parsed.netloc:
        redirected = parse.parse_qs(parsed.query).get("uddg", [""])
        if redirected and redirected[0]:
            return parse.unquote(redirected[0]).strip()
    return cleaned


def _clean_html_text(value: str) -> str:
    collapsed = _TAG_RE.sub(" ", unescape(value))
    return " ".join(collapsed.split()).strip()


def _env_timeout_seconds() -> float:
    raw = (
        os.environ.get("GWS_TUI_WEB_SEARCH_TIMEOUT_SECONDS", "").strip()
        or os.environ.get("WEB_SEARCH_TIMEOUT_SECONDS", "").strip()
    )
    if not raw:
        return DEFAULT_WEB_SEARCH_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WEB_SEARCH_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_WEB_SEARCH_TIMEOUT_SECONDS
    return value
