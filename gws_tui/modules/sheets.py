from __future__ import annotations

from datetime import datetime
from typing import Any

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule


SHEETS_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
DEFAULT_EDIT_COLUMN_COUNT = 26
DEFAULT_EDIT_ROW_COUNT = 100


def parse_timestamp(value: str) -> str:
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%b %d %I:%M %p")
    except ValueError:
        return value


def column_label(index: int) -> str:
    if index < 1:
        raise ValueError("Column index must be positive")
    label = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


def quote_sheet_title(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def values_to_grid_text(values: list[list[Any]]) -> str:
    if not values:
        return ""
    string_rows = [[("" if cell is None else str(cell)) for cell in row] for row in values]
    column_count = max(len(row) for row in string_rows)
    widths: list[int] = []
    for column_index in range(column_count):
        widths.append(
            max(
                len(row[column_index]) if column_index < len(row) else 0
                for row in string_rows
            )
        )
    lines: list[str] = []
    for row in string_rows:
        padded = []
        for column_index, width in enumerate(widths):
            value = row[column_index] if column_index < len(row) else ""
            padded.append(value.ljust(width))
        lines.append(" | ".join(padded).rstrip())
    return "\n".join(lines)


def grid_text_to_values(body: str) -> list[list[str]]:
    if not body.strip():
        return []
    rows: list[list[str]] = []
    for line in body.splitlines():
        rows.append([cell.strip() for cell in line.split("|")])
    return rows


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

    def fetch_editor_context(self, client: GwsClient, record: Record) -> dict[str, str]:
        spreadsheet = self._fetch_spreadsheet(client, record.key)
        properties = spreadsheet.get("properties", {})
        sheets = spreadsheet.get("sheets", [])
        if not sheets:
            raise ValueError("Spreadsheet has no editable tabs")
        sheet_properties = sheets[0].get("properties", {})
        sheet_title = sheet_properties.get("title", "Sheet1")
        edit_range = f"{quote_sheet_title(sheet_title)}!A1:{column_label(DEFAULT_EDIT_COLUMN_COUNT)}{DEFAULT_EDIT_ROW_COUNT}"
        values_response = client.run(
            "sheets",
            "spreadsheets",
            "values",
            "get",
            params={
                "spreadsheetId": record.key,
                "range": edit_range,
            },
        )
        return {
            "spreadsheet_id": record.key,
            "title": properties.get("title", record.title),
            "sheet_title": sheet_title,
            "clear_range": edit_range,
            "body": values_to_grid_text(values_response.get("values", [])),
        }

    def update_sheet_values(
        self,
        client: GwsClient,
        spreadsheet_id: str,
        sheet_title: str,
        clear_range: str,
        body: str,
    ) -> None:
        client.run(
            "sheets",
            "spreadsheets",
            "values",
            "clear",
            params={
                "spreadsheetId": spreadsheet_id,
                "range": clear_range,
            },
            body={},
        )
        values = grid_text_to_values(body)
        if not values:
            return
        max_columns = max(len(row) for row in values)
        update_range = f"{quote_sheet_title(sheet_title)}!A1:{column_label(max_columns)}{len(values)}"
        client.run(
            "sheets",
            "spreadsheets",
            "values",
            "update",
            params={
                "spreadsheetId": spreadsheet_id,
                "range": update_range,
                "valueInputOption": "USER_ENTERED",
            },
            body={
                "range": update_range,
                "majorDimension": "ROWS",
                "values": values,
            },
        )

    def _collect_items(self, response: dict[str, Any] | list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        if isinstance(response, list):
            items: list[dict[str, Any]] = []
            for page in response:
                items.extend(page.get(key, []))
            return items
        return response.get(key, [])

    def _fetch_spreadsheet(self, client: GwsClient, spreadsheet_id: str) -> dict[str, Any]:
        return client.run(
            "sheets",
            "spreadsheets",
            "get",
            params={
                "spreadsheetId": spreadsheet_id,
                "includeGridData": False,
            },
        )
