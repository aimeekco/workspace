from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import re

from rich.text import Text

URL_RE = re.compile(r"https?://[^\s<>()]+")
BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "div",
    "dl",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
}
SKIP_TAGS = {"head", "script", "style", "title"}


@dataclass(slots=True)
class _HtmlState:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    code: bool = False
    preformatted: bool = False
    link: str = ""
    blockquote: bool = False


class _PlainHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.href_stack: list[tuple[str, int]] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "br":
            self._newline()
            return
        if tag in BLOCK_TAGS:
            self._newline()
        if tag == "li":
            self._append("- ")
        if tag == "a":
            href = dict(attrs).get("href", "") or ""
            self.href_stack.append((href, len(self.parts)))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "a" and self.href_stack:
            href, start_index = self.href_stack.pop()
            if href:
                anchor_text = "".join(self.parts[start_index:]).strip()
                if not anchor_text or href not in anchor_text:
                    if anchor_text and not anchor_text.endswith(" "):
                        self._append(" ")
                    self._append(f"({href})")
        if tag in BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        cleaned = re.sub(r"\s+", " ", unescape(data))
        if cleaned:
            self._append(cleaned)

    def get_text(self) -> str:
        joined = "".join(self.parts).replace("\r", "")
        lines = [line.rstrip() for line in joined.splitlines()]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _append(self, value: str) -> None:
        if not value:
            return
        if value == " " and (not self.parts or self.parts[-1].endswith((" ", "\n"))):
            return
        self.parts.append(value)

    def _newline(self) -> None:
        if not self.parts:
            return
        if self.parts[-1].endswith("\n\n"):
            return
        if self.parts[-1].endswith("\n"):
            self.parts.append("\n")
            return
        self.parts.append("\n")


class _RichHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text = Text()
        self.stack: list[tuple[str, _HtmlState]] = [("root", _HtmlState())]
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "br":
            self._append_text("\n")
            return
        if tag in BLOCK_TAGS:
            self._ensure_block_break()
        current = self.stack[-1][1]
        state = _HtmlState(
            bold=current.bold,
            italic=current.italic,
            underline=current.underline,
            code=current.code,
            preformatted=current.preformatted,
            link=current.link,
            blockquote=current.blockquote,
        )
        if tag in {"b", "strong", "h1", "h2", "h3", "h4", "h5", "h6"}:
            state.bold = True
        if tag in {"em", "i"}:
            state.italic = True
        if tag == "u":
            state.underline = True
        if tag in {"code", "pre"}:
            state.code = True
        if tag == "pre":
            state.preformatted = True
        if tag == "blockquote":
            state.blockquote = True
        if tag == "a":
            state.link = dict(attrs).get("href", "") or current.link
            state.underline = True
        if tag == "li":
            self._append_text("• ", state)
        self.stack.append((tag, state))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in BLOCK_TAGS:
            self._ensure_block_break()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        state = self.stack[-1][1]
        content = unescape(data)
        if not state.preformatted:
            content = re.sub(r"\s+", " ", content)
        if not content:
            return
        if state.blockquote and self._at_line_start():
            self._append_text("> ", state)
        self._append_text(content, state)

    def get_text(self) -> Text:
        if self.text.spans:
            trimmed = self.text.copy()
            trimmed.rstrip()
            return trimmed
        plain = self.text.plain.replace("\r", "")
        lines = [line.rstrip() for line in plain.splitlines()]
        normalized = "\n".join(lines)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
        return linkify_text(normalized)

    def _append_text(self, value: str, state: _HtmlState | None = None) -> None:
        if not value:
            return
        active = state or self.stack[-1][1]
        style_parts: list[str] = []
        if active.bold:
            style_parts.append("bold")
        if active.italic:
            style_parts.append("italic")
        if active.underline:
            style_parts.append("underline")
        if active.code:
            style_parts.extend(["#ebcb8b", "on #2a2a2a"])
        elif active.link:
            style_parts.append("#88c0d0")
        elif active.blockquote:
            style_parts.append("#a7adba")
        if active.link:
            style_parts.append(f"link {active.link}")
        self.text.append(value, style=" ".join(style_parts) or None)

    def _ensure_block_break(self) -> None:
        if not self.text.plain:
            return
        if self.text.plain.endswith("\n\n"):
            return
        if self.text.plain.endswith("\n"):
            self.text.append("\n")
            return
        self.text.append("\n")

    def _at_line_start(self) -> bool:
        return not self.text.plain or self.text.plain.endswith("\n")


def html_to_text(value: str) -> str:
    parser = _PlainHtmlParser()
    parser.feed(value)
    parser.close()
    return parser.get_text()


def html_to_rich_text(value: str) -> Text:
    parser = _RichHtmlParser()
    parser.feed(value)
    parser.close()
    return parser.get_text()


def linkify_text(value: str) -> Text:
    text = Text()
    cursor = 0
    for match in URL_RE.finditer(value):
        start, end = match.span()
        candidate = match.group(0)
        trimmed = candidate.rstrip(").,!?;:")
        trailing = candidate[len(trimmed) :]
        text.append(value[cursor:start])
        text.append(trimmed, style=f"underline #88c0d0 link {trimmed}")
        if trailing:
            text.append(trailing)
        cursor = end
    text.append(value[cursor:])
    return text


def extract_links(value: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(value):
        candidate = match.group(0).rstrip(").,!?;:")
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        links.append(candidate)
    return links
