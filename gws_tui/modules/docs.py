from __future__ import annotations

from datetime import datetime
from typing import Any

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule


DOCS_MIME_TYPE = "application/vnd.google-apps.document"


def parse_timestamp(value: str) -> str:
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%b %d %I:%M %p")
    except ValueError:
        return value


def extract_document_text(document: dict[str, Any]) -> str:
    body = document.get("body", {})
    content = body.get("content", [])
    lines: list[str] = []
    for block in content:
        paragraph = block.get("paragraph")
        if paragraph:
            parts: list[str] = []
            for element in paragraph.get("elements", []):
                text_run = element.get("textRun")
                if text_run:
                    parts.append(text_run.get("content", ""))
            paragraph_text = "".join(parts).strip()
            if paragraph_text:
                lines.append(paragraph_text)
        table = block.get("table")
        if table:
            for row in table.get("tableRows", []):
                row_text: list[str] = []
                for cell in row.get("tableCells", []):
                    cell_text = extract_document_text({"body": {"content": cell.get("content", [])}}).strip()
                    if cell_text:
                        row_text.append(cell_text)
                if row_text:
                    lines.append(" | ".join(row_text))
    return "\n\n".join(lines).strip()


class DocsModule(WorkspaceModule):
    id = "docs"
    title = "Docs"
    description = "Recent Google Docs from Drive."
    columns = ("Title", "Owner", "Modified")
    empty_message = "No Google Docs found."

    def fetch_records(self, client: GwsClient) -> list[Record]:
        response = client.run(
            "drive",
            "files",
            "list",
            params={
                "q": f"mimeType='{DOCS_MIME_TYPE}' and trashed=false",
                "pageSize": 25,
                "orderBy": "modifiedTime desc",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            },
            page_all=True,
        )
        files = self._collect_items(response, "files")
        records: list[Record] = []
        for item in files:
            owners = item.get("owners", [])
            owner = owners[0].get("displayName", "Unknown") if owners else "Unknown"
            title = item.get("name", "Untitled document")
            modified = parse_timestamp(item.get("modifiedTime", ""))
            preview = "\n".join(
                [
                    f"Title: {title}",
                    f"Owner: {owner}",
                    f"Modified: {modified}",
                    "",
                    item.get("webViewLink", ""),
                ]
            ).strip()
            records.append(
                Record(
                    key=item["id"],
                    columns=(title, owner, modified),
                    title=title,
                    subtitle=owner,
                    preview=preview,
                    raw=item,
                )
            )
        return records

    def fetch_detail(self, client: GwsClient, record: Record) -> str:
        document = client.run(
            "docs",
            "documents",
            "get",
            params={
                "documentId": record.key,
                "includeTabsContent": False,
            },
        )
        text = extract_document_text(document)
        lines = [
            f"Title: {document.get('title', record.title)}",
            f"Owner: {record.subtitle or 'Unknown'}",
            f"Link: {record.raw.get('webViewLink', 'n/a')}",
            "",
            text or "(No document text found)",
        ]
        return "\n".join(lines)

    def _collect_items(self, response: dict[str, Any] | list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        if isinstance(response, list):
            items: list[dict[str, Any]] = []
            for page in response:
                items.extend(page.get(key, []))
            return items
        return response.get(key, [])
