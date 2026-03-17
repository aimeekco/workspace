from __future__ import annotations

from datetime import datetime
from typing import Any

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule


def parse_timestamp(value: str) -> str:
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%b %d %I:%M %p")
    except ValueError:
        return value


def drive_kind(item: dict[str, Any]) -> str:
    mime_type = item.get("mimeType", "")
    if mime_type == "application/vnd.google-apps.folder":
        return "Folder"
    if mime_type == "application/vnd.google-apps.document":
        return "Doc"
    if mime_type == "application/vnd.google-apps.spreadsheet":
        return "Sheet"
    if mime_type == "application/vnd.google-apps.presentation":
        return "Slide"
    if mime_type == "application/pdf":
        return "PDF"
    if mime_type.startswith("image/"):
        return "Image"
    if mime_type.startswith("video/"):
        return "Video"
    if mime_type.startswith("audio/"):
        return "Audio"
    return "File"


class DriveModule(WorkspaceModule):
    id = "drive"
    title = "Drive"
    description = "Files and folders from My Drive."
    columns = ("Name", "Kind", "Modified")
    empty_message = "No Drive files found."

    def __init__(self) -> None:
        self.current_folder_id = "root"
        self.current_folder_name = "My Drive"
        self.folder_stack: list[tuple[str, str]] = []

    def badge(self) -> str:
        return "Files"

    def loading_message(self) -> str:
        return f"Loading {self.current_folder_name}..."

    def empty_hint(self) -> str:
        return "Open a folder with Enter or refresh to reload this location."

    def list_label(self) -> str:
        return self.current_folder_name

    def fetch_records(self, client: GwsClient) -> list[Record]:
        response = client.run(
            "drive",
            "files",
            "list",
            params={
                "q": f"'{self.current_folder_id}' in parents and trashed=false",
                "pageSize": 25,
                "orderBy": "modifiedTime desc",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            },
            page_all=True,
        )
        files = self._collect_items(response, "files")
        records: list[Record] = []
        if self.folder_stack:
            records.append(
                Record(
                    key="__drive_parent__",
                    columns=("..", "Folder", ""),
                    title="..",
                    subtitle="Parent folder",
                    preview="Go back to the parent folder.\n\nPress Enter to open it.",
                    raw={"navigate_up": True},
                )
            )
        for item in files:
            owners = item.get("owners", [])
            owner = owners[0].get("displayName", "Unknown") if owners else "Unknown"
            name = item.get("name", "Untitled file")
            kind = drive_kind(item)
            modified = parse_timestamp(item.get("modifiedTime", ""))
            preview_lines = [
                f"Name: {name}",
                f"Kind: {kind}",
                f"Owner: {owner}",
                f"Modified: {modified}",
            ]
            if kind == "Folder":
                preview_lines.extend(["", "Press Enter to open this folder."])
            else:
                preview_lines.extend(["", "Press Enter to open this file in the internal viewer."])
            records.append(
                Record(
                    key=item["id"],
                    columns=(name, kind, modified),
                    title=name,
                    subtitle=kind,
                    preview="\n".join(preview_lines).strip(),
                    raw=item,
                )
            )
        return records

    def enter_folder(self, record: Record) -> None:
        self.folder_stack.append((self.current_folder_id, self.current_folder_name))
        self.current_folder_id = record.key
        self.current_folder_name = record.title

    def navigate_up(self) -> None:
        if not self.folder_stack:
            return
        self.current_folder_id, self.current_folder_name = self.folder_stack.pop()

    def reset_state(self) -> None:
        self.current_folder_id = "root"
        self.current_folder_name = "My Drive"
        self.folder_stack = []

    def fetch_detail(self, client: GwsClient, record: Record) -> str:
        item = client.run(
            "drive",
            "files",
            "get",
            params={
                "fileId": record.key,
                "supportsAllDrives": True,
            },
        )
        owners = item.get("owners", [])
        owner = owners[0].get("displayName", "Unknown") if owners else "Unknown"
        lines = [
            "Drive File Overview",
            "",
            f"Name: {item.get('name', record.title)}",
            f"Kind: {drive_kind(item)}",
            f"Owner: {owner}",
            f"Modified: {parse_timestamp(item.get('modifiedTime', ''))}",
            f"Link: {item.get('webViewLink', 'n/a')}",
        ]
        size = item.get("size")
        if size:
            lines.append(f"Size: {size} bytes")
        parents = item.get("parents", [])
        if parents:
            lines.append(f"Parents: {', '.join(parents)}")
        return "\n".join(lines)

    def _collect_items(self, response: dict[str, Any] | list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        if isinstance(response, list):
            items: list[dict[str, Any]] = []
            for page in response:
                items.extend(page.get(key, []))
            return items
        return response.get(key, [])
