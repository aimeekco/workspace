from __future__ import annotations

from datetime import datetime
from typing import Any

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule


SHEETS_MIME_TYPE = "application/vnd.google-apps.spreadsheet"


def parse_timestamp(value: str) -> str:
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%b %d %I:%M %p")
    except ValueError:
        return value


class SheetsModule(WorkspaceModule):
    id = "sheets"
    title = "Sheets"
    description = "Recent Google Sheets from Drive."
    columns = ("Title", "Owner", "Modified")
    empty_message = "No Google Sheets found."

    def badge(self) -> str:
        return "Cells"

    def loading_message(self) -> str:
        return "Loading recent spreadsheets..."

    def empty_hint(self) -> str:
        return "Refresh to reload your recent sheets."

    def fetch_records(self, client: GwsClient) -> list[Record]:
        response = client.run(
            "drive",
            "files",
            "list",
            params={
                "q": f"mimeType='{SHEETS_MIME_TYPE}' and trashed=false",
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
            title = item.get("name", "Untitled spreadsheet")
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
        spreadsheet = client.run(
            "sheets",
            "spreadsheets",
            "get",
            params={
                "spreadsheetId": record.key,
                "includeGridData": False,
            },
        )
        properties = spreadsheet.get("properties", {})
        sheets = spreadsheet.get("sheets", [])
        named_ranges = spreadsheet.get("namedRanges", [])
        lines = [
            "Spreadsheet Overview",
            "",
            f"Title: {properties.get('title', record.title)}",
            f"Owner: {record.subtitle or 'Unknown'}",
            f"Link: {record.raw.get('webViewLink', 'n/a')}",
            f"Locale: {properties.get('locale', 'Unknown')}",
            f"Time zone: {properties.get('timeZone', 'Unknown')}",
            f"Tabs: {len(sheets)}",
            f"Named ranges: {len(named_ranges)}",
            "",
            "Worksheets",
            "",
        ]
        if not sheets:
            lines.append("(No worksheet tabs found)")
            return "\n".join(lines)

        for sheet in sheets:
            sheet_properties = sheet.get("properties", {})
            grid = sheet_properties.get("gridProperties", {})
            row_count = grid.get("rowCount", "?")
            column_count = grid.get("columnCount", "?")
            frozen_rows = grid.get("frozenRowCount", 0)
            frozen_columns = grid.get("frozenColumnCount", 0)
            lines.append(
                f"- {sheet_properties.get('title', 'Untitled tab')} ({row_count}x{column_count}, frozen {frozen_rows}r/{frozen_columns}c)"
            )
        return "\n".join(lines)

    def _collect_items(self, response: dict[str, Any] | list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        if isinstance(response, list):
            items: list[dict[str, Any]] = []
            for page in response:
                items.extend(page.get(key, []))
            return items
        return response.get(key, [])
