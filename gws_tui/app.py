from __future__ import annotations

import calendar as calendar_lib
from collections import deque
from datetime import date
from datetime import datetime
import os
import time

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer
from textual.coordinate import Coordinate
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, ContentSwitcher, DataTable, Footer, Header, Input, ListItem, ListView, Static, TextArea

from gws_tui.client import GwsClient, GwsCommandEvent, GwsError
from gws_tui.models import Record
from gws_tui.modules import WorkspaceModule, built_in_modules
from gws_tui.modules.calendar import CalendarModule
from gws_tui.modules.drive import DriveModule
from gws_tui.modules.docs import DocsModule
from gws_tui.modules.gmail import GmailModule
from gws_tui.modules.sheets import SheetsModule
from gws_tui.modules.tasks import TasksModule
from gws_tui.modules.today import SECTION_LABELS, SECTION_ORDER, TodayDashboard, TodayModule
from gws_tui.profiles import GwsProfile, GwsProfileDiagnostic, discover_profiles, inspect_profile, inspect_profile_local
from gws_tui.planner import task_create_defaults

MODULE_ACCENTS = {
    "today": "#d9a441",
    "gmail": "#88c0d0",
    "calendar": "#a3be8c",
    "tasks": "#d08770",
    "drive": "#81a1c1",
    "sheets": "#b8d78d",
    "docs": "#ebcb8b",
}

PROFILE_DIAGNOSTICS_TTL_SECONDS = 30.0


class PassiveScrollableContainer(ScrollableContainer):
    """Scrollable container that doesn't participate in keyboard focus order."""

    can_focus = False


class FocusableScrollableContainer(ScrollableContainer):
    """Scrollable container that can receive focus for keyboard scrolling."""

    can_focus = True


class ProfilePickerScreen(ModalScreen[str | None]):
    """Choose an authenticated gws profile."""

    BINDINGS = [("escape", "cancel", "Cancel"), ("enter", "select_profile", "Select")]

    def __init__(
        self,
        profiles: list[GwsProfile],
        diagnostics: dict[str, GwsProfileDiagnostic],
        current_profile_name: str | None,
        title: str = "Switch Workspace Profile",
    ) -> None:
        super().__init__()
        self.profiles = profiles
        self.diagnostics = diagnostics
        self.current_profile_name = current_profile_name
        self.title = title

    def compose(self) -> ComposeResult:
        with Container(id="profile-modal", classes="modal-window"):
            yield Static(self.title, classes="modal-title")
            yield Static("Each profile maps to a separate gws config directory.", classes="modal-subtitle")
            with Horizontal(id="profile-body"):
                yield ListView(
                    *(
                        ListItem(
                            Static(self._profile_label(profile), classes="profile-item"),
                            name=profile.name,
                        )
                        for profile in self.profiles
                    ),
                    id="profile-list",
                )
                with PassiveScrollableContainer(id="profile-detail-pane"):
                    yield Static("", id="profile-detail")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="profile-cancel")
                yield Button("Use Profile", variant="primary", id="profile-submit")

    def on_mount(self) -> None:
        profile_list = self.query_one("#profile-list", ListView)
        default_index = 0
        for index, profile in enumerate(self.profiles):
            if profile.name == self.current_profile_name:
                default_index = index
                break
        profile_list.index = default_index
        profile_list.focus()
        self._update_detail(self.profiles[default_index].name if self.profiles else None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select_profile(self) -> None:
        selected = self._selected_name()
        self.dismiss(selected)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "profile-cancel":
            self.dismiss(None)
            return
        if event.button.id == "profile-submit":
            self.action_select_profile()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "profile-list":
            return
        self.action_select_profile()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "profile-list" or event.item is None:
            return
        self._update_detail(event.item.name)

    def _selected_name(self) -> str | None:
        profile_list = self.query_one("#profile-list", ListView)
        if profile_list.index is None or not self.profiles:
            return None
        return self.profiles[profile_list.index].name

    def _profile_label(self, profile: GwsProfile) -> Text:
        diagnostic = self.diagnostics.get(profile.name)
        status = diagnostic.status if diagnostic is not None else "Unknown"
        text = Text()
        text.append(profile.name, style="bold #f2f2f2")
        if profile.name == self.current_profile_name:
            text.append("  current", style="bold #a3be8c")
        text.append("\n")
        text.append(status, style=f"bold {self._status_color(status)}")
        if diagnostic is not None:
            text.append("  ")
            text.append(self._shorten_path(diagnostic.config_dir), style="#8f959f")
        return text

    def _update_detail(self, profile_name: str | None) -> None:
        if profile_name is None:
            self.query_one("#profile-detail", Static).update("")
            return
        diagnostic = self.diagnostics.get(profile_name)
        if diagnostic is None:
            self.query_one("#profile-detail", Static).update("No profile diagnostics available.")
            return
        detail = Text()
        detail.append(diagnostic.name, style="bold #f2f2f2")
        if diagnostic.name == self.current_profile_name:
            detail.append("  current", style="bold #a3be8c")
        detail.append("\n")
        detail.append(diagnostic.status, style=f"bold {self._status_color(diagnostic.status)}")
        detail.append("\n\n")

        detail.append("Config dir", style="bold #d8dee9")
        detail.append(f"\n{diagnostic.config_dir}\n\n", style="#a7adba")

        detail.append("Checks", style="bold #d8dee9")
        detail.append("\n")
        detail.append(self._flag("OAuth client", diagnostic.client_config_exists))
        detail.append("\n")
        detail.append(self._flag("Saved credentials", diagnostic.encrypted_credentials_exists))
        detail.append("\n")
        detail.append(self._flag("Refresh token", diagnostic.has_refresh_token))
        detail.append("\n")
        probe_ok = diagnostic.probe_ok
        probe_state = "ok" if probe_ok else ("pending" if diagnostic.status == "Checking..." else "failed")
        detail.append("Request probe", style="#d8dee9")
        detail.append(": ")
        detail.append(probe_state, style=f"bold {self._probe_color(probe_state)}")
        detail.append("\n")
        if diagnostic.project_id:
            detail.append("Project", style="#d8dee9")
            detail.append(f": {diagnostic.project_id}\n", style="#a7adba")
        if diagnostic.probe_message:
            detail.append("\nProbe detail\n", style="bold #d8dee9")
            detail.append(diagnostic.probe_message, style="#a7adba")
        elif diagnostic.detail:
            detail.append("\nNotes\n", style="bold #d8dee9")
            detail.append(diagnostic.detail, style="#a7adba")
        self.query_one("#profile-detail", Static).update(
            Panel(
                detail,
                title="Auth Diagnostics",
                border_style=self._status_color(diagnostic.status),
                box=box.ROUNDED,
            )
        )

    def update_diagnostic(self, diagnostic: GwsProfileDiagnostic) -> None:
        self.diagnostics[diagnostic.name] = diagnostic
        try:
            self._rerender_profile_list()
            self._update_detail(self._selected_name())
        except Exception:  # noqa: BLE001
            return

    def _rerender_profile_list(self) -> None:
        profile_list = self.query_one("#profile-list", ListView)
        selected_name = self._selected_name()
        profile_list.clear()
        selected_index = 0
        for index, profile in enumerate(self.profiles):
            profile_list.append(ListItem(Static(self._profile_label(profile), classes="profile-item"), name=profile.name))
            if profile.name == selected_name:
                selected_index = index
        if self.profiles:
            profile_list.index = selected_index

    def _status_color(self, status: str) -> str:
        normalized = status.lower()
        if "ready" in normalized:
            return "#a3be8c"
        if "checking" in normalized:
            return "#88c0d0"
        if "request failed" in normalized:
            return "#bf616a"
        if "oauth" in normalized or "login required" in normalized:
            return "#ebcb8b"
        return "#d8dee9"

    def _probe_color(self, state: str) -> str:
        if state == "ok":
            return "#a3be8c"
        if state == "pending":
            return "#88c0d0"
        return "#bf616a"

    def _flag(self, label: str, value: bool) -> Text:
        text = Text()
        text.append(label, style="#d8dee9")
        text.append(": ")
        text.append("yes" if value else "no", style=f"bold {'#a3be8c' if value else '#bf616a'}")
        return text

    def _shorten_path(self, path: str) -> str:
        home = os.path.expanduser("~")
        if path.startswith(home):
            return path.replace(home, "~", 1)
        return path


class ComposeEmailScreen(ModalScreen[dict[str, str] | None]):
    """Compose a plain text email."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str = "Compose Email",
        subtitle: str = "Send a plain text Gmail message.",
        submit_label: str = "Send",
        initial_to: str = "",
        initial_cc: str = "",
        initial_subject: str = "",
        initial_body: str = "",
        initial_attachments: str = "",
    ) -> None:
        super().__init__()
        self.title = title
        self.subtitle = subtitle
        self.submit_label = submit_label
        self.initial_to = initial_to
        self.initial_cc = initial_cc
        self.initial_subject = initial_subject
        self.initial_body = initial_body
        self.initial_attachments = initial_attachments

    def compose(self) -> ComposeResult:
        with Container(id="compose-modal", classes="modal-window"):
            yield Static(self.title, classes="modal-title")
            yield Static(self.subtitle, classes="modal-subtitle")
            yield Input(value=self.initial_to, placeholder="recipient@example.com", id="compose-to")
            yield Input(value=self.initial_cc, placeholder="cc@example.com, teammate@example.com", id="compose-cc")
            yield Input(value=self.initial_subject, placeholder="Subject", id="compose-subject")
            yield Input(
                value=self.initial_attachments,
                placeholder="Optional attachments: /path/file.pdf, /path/image.png",
                id="compose-attachments",
            )
            yield TextArea(self.initial_body, id="compose-body")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="compose-cancel")
                yield Button("Save Draft", id="compose-draft")
                yield Button(self.submit_label, variant="primary", id="compose-send")

    def on_mount(self) -> None:
        self.query_one("#compose-to", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "compose-cancel":
            self.dismiss(None)
            return
        if event.button.id not in {"compose-send", "compose-draft"}:
            return
        to = self.query_one("#compose-to", Input).value.strip()
        cc = self.query_one("#compose-cc", Input).value.strip()
        subject = self.query_one("#compose-subject", Input).value.strip()
        attachment_text = self.query_one("#compose-attachments", Input).value.strip()
        body = self.query_one("#compose-body", TextArea).text.strip()
        is_send = event.button.id == "compose-send"
        if is_send and not to:
            self.app.update_status("Compose: recipient is required")
            self.query_one("#compose-to", Input).focus()
            return
        if is_send and not subject:
            self.app.update_status("Compose: subject is required")
            self.query_one("#compose-subject", Input).focus()
            return
        if is_send and not body:
            self.app.update_status("Compose: body is required")
            self.query_one("#compose-body", TextArea).focus()
            return
        self.dismiss(
            {
                "action": "send" if is_send else "draft",
                "to": to,
                "cc": cc,
                "subject": subject,
                "body": body,
                "attachments": attachment_text,
            }
        )


class CreateEventScreen(ModalScreen[dict[str, str] | None]):
    """Create a calendar event."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        calendar_id: str,
        initial_start: str = "",
        initial_end: str = "",
        initial_duration: str = "60",
    ) -> None:
        super().__init__()
        self.calendar_id = calendar_id
        self.initial_start = initial_start
        self.initial_end = initial_end
        self.initial_duration = initial_duration

    def compose(self) -> ComposeResult:
        with Container(id="event-modal", classes="modal-window"):
            yield Static("Create Calendar Event", classes="modal-title")
            yield Static("Use local 24-hour time: YYYY-MM-DD HH:MM. End is optional if duration is set.", classes="modal-subtitle")
            yield Input(value=self.calendar_id, placeholder="primary", id="event-calendar")
            yield Input(placeholder="Title", id="event-summary")
            with Horizontal(classes="modal-row"):
                yield Input(value=self.initial_start, placeholder="2026-03-06 09:00", id="event-start")
                yield Input(value=self.initial_end, placeholder="2026-03-06 10:00", id="event-end")
            yield Input(value=self.initial_duration, placeholder="60 or 1h30m", id="event-duration")
            yield Input(placeholder="Location", id="event-location")
            yield TextArea("", id="event-description")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="event-cancel")
                yield Button("Create", variant="primary", id="event-create")

    def on_mount(self) -> None:
        self.query_one("#event-summary", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "event-cancel":
            self.dismiss(None)
            return
        if event.button.id != "event-create":
            return
        values = {
            "calendar_id": self.query_one("#event-calendar", Input).value.strip() or "primary",
            "summary": self.query_one("#event-summary", Input).value.strip(),
            "start": self.query_one("#event-start", Input).value.strip(),
            "end": self.query_one("#event-end", Input).value.strip(),
            "duration": self.query_one("#event-duration", Input).value.strip(),
            "location": self.query_one("#event-location", Input).value.strip(),
            "description": self.query_one("#event-description", TextArea).text.strip(),
        }
        if not values["summary"]:
            self.app.update_status("Event: title is required")
            self.query_one("#event-summary", Input).focus()
            return
        if not values["start"]:
            self.app.update_status("Event: start time is required")
            self.query_one("#event-start", Input).focus()
            return
        if not values["end"] and not values["duration"]:
            self.app.update_status("Event: set an end time or duration")
            self.query_one("#event-duration", Input).focus()
            return
        self.dismiss(values)


class CreateTaskScreen(ModalScreen[dict[str, str] | None]):
    """Create a Google Task."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, tasklist_id: str, tasklist_name: str) -> None:
        super().__init__()
        self.tasklist_id = tasklist_id
        self.tasklist_name = tasklist_name

    def compose(self) -> ComposeResult:
        with Container(id="task-modal", classes="modal-window"):
            yield Static("Create Task", classes="modal-title")
            yield Static(f"Task list: {self.tasklist_name}", classes="modal-subtitle")
            yield Input(placeholder="Task title", id="task-title")
            yield Input(placeholder="Optional due date: YYYY-MM-DD", id="task-due")
            yield TextArea("", id="task-notes")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="task-cancel")
                yield Button("Create", variant="primary", id="task-create")

    def on_mount(self) -> None:
        self.query_one("#task-title", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "task-cancel":
            self.dismiss(None)
            return
        if event.button.id != "task-create":
            return
        title = self.query_one("#task-title", Input).value.strip()
        if not title:
            self.app.update_status("Task: title is required")
            self.query_one("#task-title", Input).focus()
            return
        self.dismiss(
            {
                "tasklist_id": self.tasklist_id,
                "title": title,
                "due": self.query_one("#task-due", Input).value.strip(),
                "notes": self.query_one("#task-notes", TextArea).text.strip(),
            }
        )


class DeleteCalendarEventScreen(ModalScreen[str | None]):
    """Choose a calendar event from the selected day to delete."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "delete_selected", "Delete"),
    ]

    def __init__(self, day_label: str, records: list[Record]) -> None:
        super().__init__()
        self.day_label = day_label
        self.records = records

    def compose(self) -> ComposeResult:
        with Container(id="calendar-delete-modal", classes="modal-window"):
            yield Static("Delete Calendar Event", classes="modal-title")
            yield Static(self.day_label, classes="modal-subtitle")
            yield Static("Use Up/Down to choose an event. Enter deletes it. Tab moves to the buttons.", classes="modal-subtitle")
            yield ListView(
                *(
                    ListItem(
                        Static(f"{record.columns[0]}  {record.title}"),
                        name=record.key,
                    )
                    for record in self.records
                ),
                id="calendar-delete-list",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="calendar-delete-cancel")
                yield Button("Delete", variant="warning", id="calendar-delete-submit")

    def on_mount(self) -> None:
        event_list = self.query_one("#calendar-delete-list", ListView)
        event_list.index = 0
        event_list.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_delete_selected(self) -> None:
        selected = self._selected_key()
        if selected is None:
            self.dismiss(None)
            return
        self.dismiss(selected)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "calendar-delete-cancel":
            self.dismiss(None)
            return
        if event.button.id == "calendar-delete-submit":
            self.action_delete_selected()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "calendar-delete-list":
            return
        self.action_delete_selected()

    def _selected_key(self) -> str | None:
        event_list = self.query_one("#calendar-delete-list", ListView)
        if event_list.index is None or not self.records:
            return None
        return self.records[event_list.index].key


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Confirm moving a Gmail message to trash."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, subject: str) -> None:
        super().__init__()
        self.subject = subject

    def compose(self) -> ComposeResult:
        with Container(id="confirm-modal", classes="modal-window"):
            yield Static("Move Email To Trash", classes="modal-title")
            yield Static(
                f"Move this email to trash?\n{self.subject}",
                id="confirm-message",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button("Move To Trash", variant="warning", id="confirm-delete")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-delete":
            self.dismiss(True)
            return
        self.dismiss(False)


class LabelEditorScreen(ModalScreen[list[str] | None]):
    """Edit custom Gmail labels for a selected message."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, subject: str, labels: list[dict], selected_label_ids: set[str]) -> None:
        super().__init__()
        self.subject = subject
        self.labels = labels
        self.selected_label_ids = selected_label_ids

    def compose(self) -> ComposeResult:
        with Container(id="labels-modal", classes="modal-window"):
            yield Static("Edit Gmail Labels", classes="modal-title")
            yield Static(self.subject or "(No subject)", classes="modal-subtitle")
            with PassiveScrollableContainer(id="labels-list"):
                if not self.labels:
                    yield Static("No custom Gmail labels found.", id="labels-empty")
                for label in self.labels:
                    yield Checkbox(
                        label.get("name", label.get("id", "Unnamed label")),
                        value=label.get("id") in self.selected_label_ids,
                        name=label.get("id"),
                    )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="labels-cancel")
                yield Button("Apply", variant="primary", id="labels-apply")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "labels-cancel":
            self.dismiss(None)
            return
        if event.button.id != "labels-apply":
            return
        selected_ids = [checkbox.name for checkbox in self.query(Checkbox) if checkbox.value and checkbox.name]
        self.dismiss(selected_ids)


class GmailSearchScreen(ModalScreen[str | None]):
    """Set the Gmail search query for the current module."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, initial_query: str) -> None:
        super().__init__()
        self.initial_query = initial_query

    def compose(self) -> ComposeResult:
        with Container(id="gmail-search-modal", classes="modal-window"):
            yield Static("Search Gmail", classes="modal-title")
            yield Static("Blank query resets to inbox. Gmail search syntax is supported.", classes="modal-subtitle")
            yield Input(value=self.initial_query, placeholder="from:someone@example.com has:attachment", id="gmail-search-query")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="gmail-search-cancel")
                yield Button("Apply", variant="primary", id="gmail-search-apply")

    def on_mount(self) -> None:
        self.query_one("#gmail-search-query", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "gmail-search-cancel":
            self.dismiss(None)
            return
        if event.button.id != "gmail-search-apply":
            return
        query = self.query_one("#gmail-search-query", Input).value.strip()
        self.dismiss(query)


class CreateDocumentScreen(ModalScreen[dict[str, str] | None]):
    """Create a Google Doc."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Container(id="doc-create-modal", classes="modal-window"):
            yield Static("Create Google Doc", classes="modal-title")
            yield Static("Create a new document with plain-text body content.", classes="modal-subtitle")
            yield Input(placeholder="Document title", id="doc-create-title")
            yield TextArea("", id="doc-create-body")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="doc-create-cancel")
                yield Button("Create", variant="primary", id="doc-create-submit")

    def on_mount(self) -> None:
        self.query_one("#doc-create-title", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "doc-create-cancel":
            self.dismiss(None)
            return
        if event.button.id != "doc-create-submit":
            return
        title = self.query_one("#doc-create-title", Input).value.strip()
        body = self.query_one("#doc-create-body", TextArea).text
        if not title:
            self.app.update_status("Docs: title is required")
            self.query_one("#doc-create-title", Input).focus()
            return
        self.dismiss({"title": title, "body": body})


class EditDocumentScreen(ModalScreen[dict[str, str] | None]):
    """Edit Google Doc body content."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Container(id="doc-edit-modal", classes="modal-window"):
            yield Static("Edit Google Doc", classes="modal-title")
            yield Static(self.title, classes="modal-subtitle")
            yield TextArea(self.body, id="doc-edit-body")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="doc-edit-cancel")
                yield Button("Save", variant="primary", id="doc-edit-submit")

    def on_mount(self) -> None:
        self.query_one("#doc-edit-body", TextArea).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "doc-edit-cancel":
            self.dismiss(None)
            return
        if event.button.id != "doc-edit-submit":
            return
        body = self.query_one("#doc-edit-body", TextArea).text
        self.dismiss({"body": body})


class EditSheetScreen(ModalScreen[dict[str, str] | None]):
    """Edit Google Sheets cell data as aligned columns."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "save", "Save"),
        ("ctrl+o", "insert_row_below", "Row Below"),
        ("ctrl+shift+o", "insert_row_above", "Row Above"),
        ("ctrl+d", "delete_row", "Delete Row"),
        ("ctrl+g", "jump_bottom", "Bottom"),
    ]

    def __init__(self, title: str, sheet_title: str, body: str) -> None:
        super().__init__()
        self.title = title
        self.sheet_title = sheet_title
        self.body = body

    def compose(self) -> ComposeResult:
        with Container(id="sheet-edit-modal", classes="modal-window"):
            yield Static("Edit Google Sheet", classes="modal-title")
            yield Static(f"{self.title} · {self.sheet_title}", classes="modal-subtitle")
            yield Static(
                "Ctrl+S save, Ctrl+O below, Ctrl+Shift+O above, Ctrl+D delete row, Ctrl+G bottom.",
                classes="modal-subtitle",
            )
            yield Static("Keep `|` between cells; long rows scroll horizontally.", classes="modal-subtitle")
            yield TextArea(self.body, id="sheet-edit-body", soft_wrap=False)
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="sheet-edit-cancel")
                yield Button("Save", variant="primary", id="sheet-edit-submit")

    def on_mount(self) -> None:
        self.query_one("#sheet-edit-body", TextArea).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        body = self.query_one("#sheet-edit-body", TextArea).text
        self.dismiss({"body": body})

    def action_insert_row_below(self) -> None:
        self._insert_row(offset=1)

    def action_insert_row_above(self) -> None:
        self._insert_row(offset=0)

    def action_delete_row(self) -> None:
        area = self.query_one("#sheet-edit-body", TextArea)
        lines = self._editor_lines(area)
        row, column = area.cursor_location
        row = max(0, min(row, len(lines) - 1))
        if len(lines) == 1:
            lines = [self._blank_row_template(lines[0])]
            next_row = 0
        else:
            del lines[row]
            next_row = min(row, len(lines) - 1)
        self._replace_editor_text(area, lines, next_row, column)

    def action_jump_bottom(self) -> None:
        area = self.query_one("#sheet-edit-body", TextArea)
        lines = self._editor_lines(area)
        target_row = max(0, len(lines) - 1)
        target_column = min(area.cursor_location[1], len(lines[target_row]))
        area.move_cursor((target_row, target_column))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sheet-edit-cancel":
            self.dismiss(None)
            return
        if event.button.id != "sheet-edit-submit":
            return
        self.action_save()

    def _insert_row(self, offset: int) -> None:
        area = self.query_one("#sheet-edit-body", TextArea)
        lines = self._editor_lines(area)
        row, column = area.cursor_location
        row = max(0, min(row, len(lines) - 1))
        insert_at = row + offset
        template_source = lines[row] if lines else ""
        lines.insert(insert_at, self._blank_row_template(template_source))
        self._replace_editor_text(area, lines, insert_at, column)

    def _editor_lines(self, area: TextArea) -> list[str]:
        return area.text.split("\n") or [""]

    def _blank_row_template(self, source_line: str) -> str:
        if " | " not in source_line:
            return ""
        return " | ".join(" " * max(1, len(segment)) for segment in source_line.split(" | "))

    def _replace_editor_text(self, area: TextArea, lines: list[str], row: int, column: int) -> None:
        area.load_text("\n".join(lines))
        target_row = max(0, min(row, len(lines) - 1))
        target_column = min(column, len(lines[target_row]))
        area.move_cursor((target_row, target_column))
        area.focus()


class CalendarGridView(PassiveScrollableContainer):
    """Month-style calendar grid for the Calendar module."""

    def __init__(self, module: CalendarModule, client: GwsClient) -> None:
        super().__init__(id=f"view-{module.id}")
        self.module = module
        self.client = client
        self.month_anchor = date.today().replace(day=1)
        self.day_records: dict[str, list[Record]] = {}
        self.coordinate_day: dict[tuple[int, int], date | None] = {}
        self.loaded = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="module-heading"):
            yield Static(self.module.title, id="title-calendar", classes="module-title")
            yield Static(self.module.badge(), id="badge-calendar", classes="module-badge")
        yield Static("", id="calendar-month-label", classes="pane-title")
        with Horizontal(classes="module-body"):
            with Container(classes="pane pane-table"):
                yield Static("Month", classes="pane-title")
                yield DataTable(id="calendar-grid")
            with Container(classes="pane pane-detail"):
                yield Static("Day Agenda", id="detail-label-calendar", classes="pane-title")
                with FocusableScrollableContainer(classes="detail-container"):
                    yield Static("Select a day to view events.", id="detail-calendar")

    def on_mount(self) -> None:
        table = self.query_one("#calendar-grid", DataTable)
        table.cursor_type = "cell"
        table.zebra_stripes = False
        table.add_columns("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")

    def load_if_needed(self) -> None:
        if not self.loaded:
            self.action_refresh()

    def action_refresh(self) -> None:
        self.loaded = True
        self._set_detail_text(self._state_text("Loading calendar", self.module.loading_message()))
        self.app.update_status("Loading calendar...")
        self._load_month()

    def action_next_month(self) -> None:
        year = self.month_anchor.year + (1 if self.month_anchor.month == 12 else 0)
        month = 1 if self.month_anchor.month == 12 else self.month_anchor.month + 1
        self.month_anchor = date(year, month, 1)
        self.action_refresh()

    def action_previous_month(self) -> None:
        year = self.month_anchor.year - (1 if self.month_anchor.month == 1 else 0)
        month = 12 if self.month_anchor.month == 1 else self.month_anchor.month - 1
        self.month_anchor = date(year, month, 1)
        self.action_refresh()

    @work(thread=True, exclusive=True)
    def _load_month(self) -> None:
        try:
            records = self.module.fetch_month_records(self.client, self.month_anchor.year, self.month_anchor.month)
        except GwsError as exc:
            self.app.call_from_thread(self._handle_error, exc.message)
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._handle_error, str(exc))
            return
        self.app.call_from_thread(self._render_month, records)

    def _render_month(self, records: list[Record]) -> None:
        self.day_records = {}
        for record in records:
            day_keys = record.raw.get("day_keys") or []
            for day_key in day_keys:
                self.day_records.setdefault(day_key, []).append(record)

        month_label = f"{calendar_lib.month_name[self.month_anchor.month]} {self.month_anchor.year}   [ / ] to change month"
        self.query_one("#calendar-month-label", Static).update(month_label)

        table = self.query_one("#calendar-grid", DataTable)
        table.clear()
        self.coordinate_day = {}
        month_matrix = self.module.month_matrix(self.month_anchor.year, self.month_anchor.month)
        for row_index, week in enumerate(month_matrix):
            row_values: list[str] = []
            for column_index, current_day in enumerate(week):
                self.coordinate_day[(row_index, column_index)] = current_day if current_day.month == self.month_anchor.month else None
                row_values.append(self._format_day_cell(current_day))
            table.add_row(*row_values, height=5)

        today = date.today()
        selected_row = 0
        selected_column = 0
        found_selection = False
        for row_index, week in enumerate(month_matrix):
            for column_index, current_day in enumerate(week):
                if current_day == today and current_day.month == self.month_anchor.month:
                    selected_row = row_index
                    selected_column = column_index
                    found_selection = True
                    break
            if found_selection:
                break
        if not found_selection:
            for row_index, week in enumerate(month_matrix):
                for column_index, current_day in enumerate(week):
                    if current_day.month == self.month_anchor.month:
                        selected_row = row_index
                        selected_column = column_index
                        found_selection = True
                        break
                if found_selection:
                    break
        table.move_cursor(row=selected_row, column=selected_column)
        selected_day = self.coordinate_day.get((selected_row, selected_column))
        self._show_day_preview(selected_day)
        self.app.update_status(f"Calendar: loaded {calendar_lib.month_name[self.month_anchor.month]} {self.month_anchor.year}")

    def _format_day_cell(self, current_day: date) -> str:
        day_label = f"{current_day.day}"
        if current_day.month != self.month_anchor.month:
            return day_label
        entries = self.day_records.get(current_day.isoformat(), [])
        event_lines = [self._truncate_event(record.title) for record in entries[:2]]
        if len(entries) > 2:
            event_lines.append(f"+{len(entries) - 2} more")
        content = [day_label, *event_lines]
        return "\n".join(content)

    def _truncate_event(self, title: str) -> str:
        if len(title) <= 16:
            return title
        return f"{title[:13]}..."

    def _show_day_preview(self, selected_day: date | None) -> None:
        self.query_one("#detail-label-calendar", Static).update("Day Agenda")
        if selected_day is None:
            self._set_detail_text(self._state_text("Out of month", "Move to a date inside the current month to inspect its agenda."))
            return
        records = self.day_records.get(selected_day.isoformat(), [])
        if not records:
            self._set_detail_text(
                self._state_text(
                    selected_day.strftime("%A, %B %d"),
                    "No events on this day.",
                    "Press a to create one or move to another date.",
                )
            )
            return
        lines = [selected_day.strftime("%A, %B %d"), ""]
        for record in records:
            event = record.raw["event"]
            lines.append(f"{record.title}")
            lines.append(f"{record.subtitle}  {self.module.columns[0]}: {record.columns[0]}")
            if record.columns[3]:
                lines.append(f"Location: {record.columns[3]}")
            lines.append("")
        self._set_detail_text("\n".join(lines).rstrip())
        self.app.update_status("Calendar: day preview ready")

    def _handle_error(self, message: str) -> None:
        table = self.query_one("#calendar-grid", DataTable)
        table.clear()
        self._set_detail_text(self._state_text("Calendar request failed", message))
        self.app.update_status("Calendar: request failed")

    def _set_detail_text(self, value: str) -> None:
        self.query_one("#detail-calendar", Static).update(
            Panel(
                Text(value),
                title="Calendar",
                subtitle="Agenda",
                border_style=MODULE_ACCENTS["calendar"],
                box=box.ROUNDED,
            )
        )

    def _state_text(self, heading: str, message: str, hint: str = "") -> str:
        lines = [heading, "", message]
        if hint:
            lines.extend(["", hint])
        return "\n".join(lines)

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        if event.data_table.id != "calendar-grid":
            return
        self._show_day_preview(self.coordinate_day.get((event.coordinate.row, event.coordinate.column)))

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        if event.data_table.id != "calendar-grid":
            return
        self._show_day_preview(self.coordinate_day.get((event.coordinate.row, event.coordinate.column)))

    def current_record(self) -> Record | None:
        table = self.query_one("#calendar-grid", DataTable)
        selected_day = self.coordinate_day.get((table.cursor_coordinate.row, table.cursor_coordinate.column))
        if selected_day is None:
            return None
        records = self.day_records.get(selected_day.isoformat(), [])
        return records[0] if records else None

    def selected_day(self) -> date | None:
        table = self.query_one("#calendar-grid", DataTable)
        return self.coordinate_day.get((table.cursor_coordinate.row, table.cursor_coordinate.column))

    def selected_day_records(self) -> list[Record]:
        selected_day = self.selected_day()
        if selected_day is None:
            return []
        return list(self.day_records.get(selected_day.isoformat(), []))

    def reset_state(self) -> None:
        self.day_records = {}
        self.coordinate_day = {}
        self.month_anchor = date.today().replace(day=1)
        self.loaded = False


class ModuleView(PassiveScrollableContainer):
    """A reusable list/detail view for a workspace module."""

    def __init__(self, module: WorkspaceModule, client: GwsClient) -> None:
        super().__init__(id=f"view-{module.id}")
        self.module = module
        self.client = client
        self.records: dict[str, Record] = {}
        self.current_key: str | None = None
        self.detail_cache: dict[str, str] = {}
        self.detail_label = "Preview"
        self.loaded = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="module-heading"):
            yield Static(self.module.title, id=f"title-{self.module.id}", classes="module-title")
            yield Static(self.module.badge(), id=f"badge-{self.module.id}", classes="module-badge")
        with Horizontal(classes="module-body"):
            with Container(classes="pane pane-table"):
                yield Static(self.module.list_label(), id=f"list-label-{self.module.id}", classes="pane-title")
                yield DataTable(id=f"table-{self.module.id}")
            with Container(classes="pane pane-detail"):
                yield Static("Preview", id=f"detail-label-{self.module.id}", classes="pane-title")
                with FocusableScrollableContainer(classes="detail-container"):
                    yield Static(
                        "Select a row to preview. Press Enter for full detail.",
                        id=f"detail-{self.module.id}",
                    )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(*self.module.columns)
        table.show_header = True

    def load_if_needed(self) -> None:
        if not self.loaded:
            self.action_refresh()

    def action_refresh(self) -> None:
        self.loaded = True
        self.detail_cache.clear()
        self._refresh_list_label()
        self._set_detail_label("Preview")
        self._set_detail_text(self._state_text(f"Loading {self.module.title}", self.module.loading_message()))
        self.app.update_status(f"Loading {self.module.title.lower()}...")
        self._load_records()

    def _refresh_list_label(self) -> None:
        self.query_one(f"#list-label-{self.module.id}", Static).update(self.module.list_label())

    def show_preview(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        record = self.records[key]
        preview = record.preview or f"{record.title}\n{record.subtitle}".strip()
        self._set_detail_label("Preview")
        self._set_detail_text(preview)
        self.app.update_status(f"{self.module.title}: preview ready, press Enter for full detail")

    def open_record(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        self._set_detail_label("Detail")
        if key in self.detail_cache:
            self._render_detail(self.detail_cache[key])
            return
        self._set_detail_text(self._state_text(f"Loading {self.module.title} detail", "Fetching the selected record from Google Workspace..."))
        self.app.update_status(f"Loading {self.module.title.lower()} detail...")
        self._load_detail(key)

    @work(thread=True, exclusive=True)
    def _load_records(self) -> None:
        try:
            records = self.module.fetch_records(self.client)
        except GwsError as exc:
            self.app.call_from_thread(self._handle_error, exc.message)
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._handle_error, str(exc))
            return
        self.app.call_from_thread(self._render_records, records)

    @work(thread=True, exclusive=True)
    def _load_detail(self, key: str) -> None:
        record = self.records[key]
        try:
            detail = self.module.fetch_detail(self.client, record)
        except GwsError as exc:
            self.app.call_from_thread(self._set_detail_text, self._state_text(f"{self.module.title} detail failed", exc.message))
            self.app.call_from_thread(self.app.update_status, f"{self.module.title} request failed")
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._set_detail_text, self._state_text(f"{self.module.title} detail failed", str(exc)))
            self.app.call_from_thread(self.app.update_status, f"{self.module.title} detail failed")
            return
        self.detail_cache[key] = detail
        self.app.call_from_thread(self._render_detail, detail)

    def _render_records(self, records: list[Record]) -> None:
        self.records = {record.key: record for record in records}
        table = self.query_one(DataTable)
        table.clear()
        self._refresh_list_label()
        if not records:
            self.current_key = None
            self._set_detail_label("Preview")
            self._set_detail_text(self._state_text(f"No {self.module.title.lower()} records", self.module.empty_message, self.module.empty_hint()))
            self.app.update_status(f"{self.module.title}: no records")
            return

        first_key = records[0].key
        for record in records:
            table.add_row(*record.columns, key=record.key)
        table.move_cursor(row=0, column=0)
        self.show_preview(first_key)
        self.app.update_status(f"{self.module.title}: loaded {len(records)} records")

    def _render_detail(self, detail: str) -> None:
        self.query_one(f"#detail-{self.module.id}", Static).update(
            Panel(
                Text(detail),
                title=self.module.title,
                subtitle=self._detail_label_text(),
                border_style=MODULE_ACCENTS.get(self.module.id, "#3a3a3a"),
                box=box.ROUNDED,
            )
        )
        self.app.update_status(f"{self.module.title}: detail loaded")

    def _set_detail_text(self, value: str) -> None:
        self.query_one(f"#detail-{self.module.id}", Static).update(
            Panel(
                Text(value),
                title=self.module.title,
                subtitle=self._detail_label_text(),
                border_style=MODULE_ACCENTS.get(self.module.id, "#3a3a3a"),
                box=box.ROUNDED,
            )
        )

    def _handle_error(self, message: str) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self.current_key = None
        self._set_detail_label("Preview")
        self._set_detail_text(self._state_text(f"{self.module.title} request failed", message))
        self.app.update_status(f"{self.module.title}: request failed")

    def _state_text(self, heading: str, message: str, hint: str = "") -> str:
        lines = [heading, "", message]
        if hint:
            lines.extend(["", hint])
        return "\n".join(lines)

    def _detail_label_text(self) -> str:
        return self.detail_label

    def _set_detail_label(self, value: str) -> None:
        self.detail_label = value
        self.query_one(f"#detail-label-{self.module.id}", Static).update(value)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != f"table-{self.module.id}" or event.row_key is None:
            return
        self.show_preview(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != f"table-{self.module.id}" or event.row_key is None:
            return
        self.open_record(str(event.row_key.value))

    def current_record(self) -> Record | None:
        if self.current_key is None:
            return None
        return self.records.get(self.current_key)

    def reset_state(self) -> None:
        self.records = {}
        self.current_key = None
        self.detail_cache.clear()
        self.detail_label = "Preview"
        self.loaded = False


class TodayView(PassiveScrollableContainer):
    """Gemini-powered workspace dashboard."""

    module: TodayModule

    def __init__(self, module: TodayModule, client: GwsClient) -> None:
        super().__init__(id=f"view-{module.id}")
        self.module = module
        self.client = client
        self.section_records: dict[str, list[Record]] = {}
        self.records: dict[str, Record] = {}
        self.current_key: str | None = None
        self.current_section = SECTION_ORDER[0]
        self.detail_label = "Preview"
        self.loaded = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="module-heading"):
            yield Static(self.module.title, id="title-today", classes="module-title")
            yield Static(self.module.badge(), id="badge-today", classes="module-badge")
        with Horizontal(classes="module-body"):
            with Container(id="pane-sections-today", classes="pane pane-mailboxes"):
                yield Static("Sections", classes="pane-title")
                yield ListView(id="section-list-today")
            with Container(classes="pane pane-mail"):
                yield Static(SECTION_LABELS[self.current_section], id="section-heading-today", classes="pane-title")
                yield DataTable(id="table-today")
            with Container(classes="pane pane-mail-detail"):
                yield Static("Preview", id="detail-label-today", classes="pane-title")
                with FocusableScrollableContainer(classes="detail-container"):
                    yield Static("Select a section to inspect today's briefing.", id="detail-today")

    def on_mount(self) -> None:
        table = self.query_one("#table-today", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Item", width=38)
        table.add_column("Source", width=18)
        table.add_column("When", width=18)
        table.show_header = True

    def load_if_needed(self) -> None:
        if not self.loaded:
            self.action_refresh()

    def action_refresh(self) -> None:
        self.loaded = True
        self.detail_label = "Preview"
        self._set_detail_label("Preview")
        self._set_detail_text(self._state_text("Loading Today", self.module.loading_message()))
        self.app.update_status("Loading today...")
        self._load_dashboard(False)

    def action_regenerate(self) -> None:
        self.loaded = True
        self.detail_label = "Preview"
        self._set_detail_label("Preview")
        self._set_detail_text(self._state_text("Refreshing Today", "Ignoring the cache and rebuilding the daily brief..."))
        self.app.update_status("Regenerating today...")
        self._load_dashboard(True)

    @work(thread=True, exclusive=True)
    def _load_dashboard(self, force_refresh: bool) -> None:
        try:
            dashboard = self.module.fetch_dashboard(self.client, force_refresh=force_refresh)
        except GwsError as exc:
            self.app.call_from_thread(self._handle_error, exc.message)
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._handle_error, str(exc))
            return
        self.app.call_from_thread(self._render_dashboard, dashboard)

    def _render_dashboard(self, dashboard: TodayDashboard) -> None:
        self.section_records = {section: list(dashboard.section_records.get(section, [])) for section in SECTION_ORDER}
        self._render_sections()
        self._render_section_records()
        self.app.refresh_bindings()
        warnings = f", {len(dashboard.warnings)} warnings" if dashboard.warnings else ""
        self.app.update_status(f"Today: loaded from {dashboard.source}{warnings}")

    def _render_sections(self) -> None:
        section_list = self.query_one("#section-list-today", ListView)
        section_list.clear()
        selected_index = 0
        for index, section in enumerate(SECTION_ORDER):
            count = len(self.section_records.get(section, []))
            label = f"{SECTION_LABELS[section]} ({count})"
            section_list.append(ListItem(Static(label), name=section))
            if section == self.current_section:
                selected_index = index
        section_list.index = selected_index
        self._apply_section_width()

    def _apply_section_width(self) -> None:
        longest = max([len("Sections"), *(len(f"{SECTION_LABELS[section]} ({len(self.section_records.get(section, []))})") for section in SECTION_ORDER)], default=len("Sections"))
        self.query_one("#pane-sections-today", Container).styles.width = max(18, longest + 4)

    def _render_section_records(self) -> None:
        self.query_one("#section-heading-today", Static).update(SECTION_LABELS[self.current_section])
        records = self.section_records.get(self.current_section, [])
        self.records = {record.key: record for record in records}
        table = self.query_one("#table-today", DataTable)
        table.clear()
        if not records:
            self.current_key = None
            self._set_detail_label("Preview")
            self._set_detail_text(self._state_text("No items", f"{SECTION_LABELS[self.current_section]} is empty."))
            self.app.refresh_bindings()
            return
        for record in records:
            table.add_row(*record.columns, key=record.key)
        table.move_cursor(row=0, column=0)
        self.show_preview(records[0].key)

    def show_preview(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        record = self.records[key]
        self._set_detail_label("Preview")
        self._set_detail_text(record.preview or record.title)
        self.app.refresh_bindings()
        self.app.update_status(f"Today: previewing {record.title}")

    def open_record(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        record = self.records[key]
        self._set_detail_label("Detail")
        self._set_detail_text(str(record.raw.get("detail", record.preview or record.title)))
        self.app.refresh_bindings()
        self.app.update_status(f"Today: {record.title}")

    def current_record(self) -> Record | None:
        if self.current_key is None:
            return None
        return self.records.get(self.current_key)

    def current_draft_id(self) -> str | None:
        record = self.current_record()
        if record is None:
            return None
        draft_id = record.raw.get("draft_id")
        return str(draft_id) if draft_id else None

    def sync_from_module(self) -> None:
        dashboard = self.module.current_dashboard
        if dashboard is None:
            self.action_refresh()
            return
        self._render_dashboard(dashboard)

    def _set_detail_label(self, value: str) -> None:
        self.detail_label = value
        self.query_one("#detail-label-today", Static).update(value)

    def _set_detail_text(self, value: str) -> None:
        self.query_one("#detail-today", Static).update(
            Panel(Text(value), title=self.module.title, subtitle=self.detail_label, border_style=MODULE_ACCENTS["today"], box=box.ROUNDED)
        )

    def _state_text(self, heading: str, message: str, hint: str = "") -> str:
        lines = [heading, "", message]
        if hint:
            lines.extend(["", hint])
        return "\n".join(lines)

    def _handle_error(self, message: str) -> None:
        self.records = {}
        self.current_key = None
        self._set_detail_label("Preview")
        self._set_detail_text(self._state_text("Today request failed", message))
        table = self.query_one("#table-today", DataTable)
        table.clear()
        self.app.update_status("Today: request failed")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "table-today" or event.row_key is None:
            return
        self.show_preview(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "table-today" or event.row_key is None:
            return
        self.open_record(str(event.row_key.value))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "section-list-today" or event.item is None:
            return
        section = event.item.name or self.current_section
        if section == self.current_section:
            return
        self.current_section = section
        self._render_section_records()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "section-list-today" or event.item is None:
            return
        section = event.item.name or self.current_section
        if section == self.current_section:
            return
        self.current_section = section
        self._render_section_records()

    def reset_state(self) -> None:
        self.section_records = {}
        self.records = {}
        self.current_key = None
        self.current_section = SECTION_ORDER[0]
        self.detail_label = "Preview"
        self.loaded = False


class GmailView(PassiveScrollableContainer):
    """Gmail-specific three-pane layout with mailbox selector."""

    def __init__(self, module: GmailModule, client: GwsClient) -> None:
        super().__init__(id=f"view-{module.id}")
        self.module = module
        self.client = client
        self.records: dict[str, Record] = {}
        self.current_key: str | None = None
        self.detail_cache: dict[str, str] = {}
        self.detail_label = "Preview"
        self.loaded = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="module-heading"):
            yield Static(self.module.title, id="title-gmail", classes="module-title")
            yield Static(self.module.badge(), id="badge-gmail", classes="module-badge")
        with Horizontal(classes="module-body"):
            with Container(id="pane-mailboxes-gmail", classes="pane pane-mailboxes"):
                yield Static("Mailboxes", classes="pane-title")
                yield ListView(id="mailbox-list-gmail")
            with Container(classes="pane pane-mail"):
                yield Static(self.module.list_label(), id="mailbox-heading-gmail", classes="pane-title")
                yield DataTable(id="table-gmail")
            with Container(classes="pane pane-mail-detail"):
                yield Static("Preview", id="detail-label-gmail", classes="pane-title")
                with FocusableScrollableContainer(classes="detail-container"):
                    yield Static(
                        "Select a row to preview. Press Enter for full detail.",
                        id="detail-gmail",
                    )

    def on_mount(self) -> None:
        table = self.query_one("#table-gmail", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Subject", width=38)
        table.add_column("From", width=26)
        table.add_column("Time", width=16)
        table.show_header = True

    def load_if_needed(self) -> None:
        if not self.loaded:
            self.action_refresh()

    def action_refresh(self) -> None:
        self.loaded = True
        self.detail_cache.clear()
        self._set_detail_label("Preview")
        self._refresh_mailbox_heading()
        self._set_detail_text(self._state_text("Loading Gmail", self.module.loading_message()))
        self.app.update_status("Loading gmail...")
        self._load_records()

    @work(thread=True, exclusive=True)
    def _load_records(self) -> None:
        try:
            records = self.module.fetch_records(self.client)
        except GwsError as exc:
            self.app.call_from_thread(self._handle_error, exc.message)
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._handle_error, str(exc))
            return
        self.app.call_from_thread(self._render_records, records)

    @work(thread=True, exclusive=True)
    def _load_detail(self, key: str) -> None:
        record = self.records[key]
        try:
            detail = self.module.fetch_detail(self.client, record)
        except GwsError as exc:
            self.app.call_from_thread(self._set_detail_text, self._state_text("Gmail detail failed", exc.message))
            self.app.call_from_thread(self.app.update_status, "Gmail request failed")
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._set_detail_text, self._state_text("Gmail detail failed", str(exc)))
            self.app.call_from_thread(self.app.update_status, "Gmail detail failed")
            return
        self.detail_cache[key] = detail
        self.app.call_from_thread(self._refresh_row, key)
        self.app.call_from_thread(self._render_detail, detail)

    def _render_records(self, records: list[Record]) -> None:
        self._render_mailboxes()
        self._refresh_mailbox_heading()
        self.records = {record.key: record for record in records}
        table = self.query_one("#table-gmail", DataTable)
        table.clear()
        if not records:
            self.current_key = None
            self._set_detail_label("Preview")
            self._set_detail_text(self._state_text("No gmail records", self.module.empty_message, self.module.empty_hint()))
            self.app.update_status("Gmail: no records")
            return

        first_key = records[0].key
        for record in records:
            table.add_row(
                self._subject_cell(record),
                self._sender_cell(record),
                self._time_cell(record),
                key=record.key,
            )
        table.move_cursor(row=0, column=0)
        self.show_preview(first_key)
        self.app.update_status(f"Gmail: loaded {len(records)} records")

    def _render_mailboxes(self) -> None:
        mailbox_list = self.query_one("#mailbox-list-gmail", ListView)
        mailbox_list.clear()
        selected_index = 0
        for index, mailbox in enumerate(self.module.mailboxes):
            mailbox_list.append(ListItem(Static(mailbox["name"]), name=mailbox["id"]))
            if mailbox["id"] == self.module.selected_mailbox_id:
                selected_index = index
        if self.module.mailboxes:
            mailbox_list.index = selected_index
        self._apply_mailbox_width()

    def _apply_mailbox_width(self) -> None:
        longest = max(
            [len("Mailboxes"), *(len(mailbox["name"]) for mailbox in self.module.mailboxes)],
            default=len("Mailboxes"),
        )
        # Account for the pane border and a small amount of inner padding.
        width = max(15, longest + 5)
        self.query_one("#pane-mailboxes-gmail", Container).styles.width = width

    def _refresh_mailbox_heading(self) -> None:
        self.query_one("#mailbox-heading-gmail", Static).update(self.module.list_label())

    def show_preview(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        record = self.records[key]
        preview = record.preview or f"{record.title}\n{record.subtitle}".strip()
        self._set_detail_label("Preview")
        self._set_detail_text(preview)
        self.app.update_status("Gmail: preview ready, press Enter for full detail")

    def open_record(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        self._set_detail_label("Detail")
        if key in self.detail_cache:
            self._render_detail(self.detail_cache[key])
            return
        self._set_detail_text(self._state_text("Loading Gmail detail", "Fetching the selected message or thread..."))
        self.app.update_status("Loading gmail detail...")
        self._load_detail(key)

    def _render_detail(self, detail: str) -> None:
        self.query_one("#detail-gmail", Static).update(
            Panel(
                Text(detail),
                title=self.module.title,
                subtitle=self.detail_label,
                border_style=MODULE_ACCENTS["gmail"],
                box=box.ROUNDED,
            )
        )
        self.app.update_status("Gmail: detail loaded")

    def _set_detail_text(self, value: str) -> None:
        self.query_one("#detail-gmail", Static).update(
            Panel(
                Text(value),
                title=self.module.title,
                subtitle=self.detail_label,
                border_style=MODULE_ACCENTS["gmail"],
                box=box.ROUNDED,
            )
        )

    def _handle_error(self, message: str) -> None:
        self._render_mailboxes()
        self._refresh_mailbox_heading()
        table = self.query_one("#table-gmail", DataTable)
        table.clear()
        self.current_key = None
        self._set_detail_label("Preview")
        self._set_detail_text(self._state_text("Gmail request failed", message))
        self.app.update_status("Gmail: request failed")

    def _set_detail_label(self, value: str) -> None:
        self.detail_label = value
        self.query_one("#detail-label-gmail", Static).update(value)

    def _refresh_row(self, key: str) -> None:
        record = self.records.get(key)
        if record is None:
            return
        table = self.query_one("#table-gmail", DataTable)
        row_index = table.get_row_index(key)
        table.update_cell_at(Coordinate(row_index, 0), self._subject_cell(record))
        table.update_cell_at(Coordinate(row_index, 1), self._sender_cell(record))
        table.update_cell_at(Coordinate(row_index, 2), self._time_cell(record))

    def _subject_cell(self, record: Record) -> Text:
        unread = bool(record.raw.get("unread"))
        subject = record.title or record.columns[0]
        text = Text()
        if unread:
            text.append("● ", style="#88c0d0")
            text.append(subject, style="bold #f2f2f2")
            return text
        text.append(subject, style="#d8dee9")
        return text

    def _sender_cell(self, record: Record) -> Text:
        unread = bool(record.raw.get("unread"))
        text = Text(record.columns[1])
        text.stylize("#e5e9f0" if unread else "#8f959f")
        return text

    def _time_cell(self, record: Record) -> Text:
        unread = bool(record.raw.get("unread"))
        text = Text(record.columns[2])
        text.stylize("#d8dee9" if unread else "#7f848e")
        return text

    def _state_text(self, heading: str, message: str, hint: str = "") -> str:
        lines = [heading, "", message]
        if hint:
            lines.extend(["", hint])
        return "\n".join(lines)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "table-gmail" or event.row_key is None:
            return
        self.show_preview(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "table-gmail" or event.row_key is None:
            return
        self.open_record(str(event.row_key.value))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "mailbox-list-gmail" or event.item is None:
            return
        mailbox_id = event.item.name or ""
        mailbox = next((item for item in self.module.mailboxes if item["id"] == mailbox_id), None)
        if mailbox is None or mailbox_id == self.module.selected_mailbox_id:
            return
        self.module.set_mailbox(mailbox["id"], mailbox["name"])
        self.app.update_status(f"Gmail mailbox: {mailbox['name']}")
        self.action_refresh()

    def current_record(self) -> Record | None:
        if self.current_key is None:
            return None
        return self.records.get(self.current_key)

    def reset_state(self) -> None:
        self.records = {}
        self.current_key = None
        self.detail_cache.clear()
        self.detail_label = "Preview"
        self.loaded = False


class TasksView(PassiveScrollableContainer):
    """Tasks-specific three-pane layout with task list selector."""

    def __init__(self, module: TasksModule, client: GwsClient) -> None:
        super().__init__(id=f"view-{module.id}")
        self.module = module
        self.client = client
        self.records: dict[str, Record] = {}
        self.current_key: str | None = None
        self.detail_cache: dict[str, str] = {}
        self.detail_label = "Preview"
        self.loaded = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="module-heading"):
            yield Static(self.module.title, id="title-tasks", classes="module-title")
            yield Static(self.module.badge(), id="badge-tasks", classes="module-badge")
        with Horizontal(classes="module-body"):
            with Container(id="pane-tasklists-tasks", classes="pane pane-mailboxes"):
                yield Static("Task Lists", classes="pane-title")
                yield ListView(id="tasklist-list-tasks")
            with Container(classes="pane pane-mail"):
                yield Static(self.module.list_label(), id="tasklist-heading-tasks", classes="pane-title")
                yield DataTable(id="table-tasks")
            with Container(classes="pane pane-mail-detail"):
                yield Static("Preview", id="detail-label-tasks", classes="pane-title")
                with FocusableScrollableContainer(classes="detail-container"):
                    yield Static("Select a task to preview. Press Enter for full detail.", id="detail-tasks")

    def on_mount(self) -> None:
        table = self.query_one("#table-tasks", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Task", width=34)
        table.add_column("List", width=20)
        table.add_column("Due", width=12)
        table.show_header = True

    def load_if_needed(self) -> None:
        if not self.loaded:
            self.action_refresh()

    def action_refresh(self) -> None:
        self.loaded = True
        self.detail_cache.clear()
        self._set_detail_label("Preview")
        self._refresh_tasklist_heading()
        self._set_detail_text(self._state_text("Loading Tasks", self.module.loading_message()))
        self.app.update_status("Loading tasks...")
        self._load_records()

    @work(thread=True, exclusive=True)
    def _load_records(self) -> None:
        try:
            records = self.module.fetch_records(self.client)
        except GwsError as exc:
            self.app.call_from_thread(self._handle_error, exc.message)
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._handle_error, str(exc))
            return
        self.app.call_from_thread(self._render_records, records)

    @work(thread=True, exclusive=True)
    def _load_detail(self, key: str) -> None:
        record = self.records[key]
        try:
            detail = self.module.fetch_detail(self.client, record)
        except GwsError as exc:
            self.app.call_from_thread(self._set_detail_text, self._state_text("Tasks detail failed", exc.message))
            self.app.call_from_thread(self.app.update_status, "Tasks request failed")
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._set_detail_text, self._state_text("Tasks detail failed", str(exc)))
            self.app.call_from_thread(self.app.update_status, "Tasks detail failed")
            return
        self.detail_cache[key] = detail
        self.app.call_from_thread(self._render_detail, detail)

    def _render_records(self, records: list[Record]) -> None:
        self._render_tasklists()
        self._refresh_tasklist_heading()
        self.records = {record.key: record for record in records}
        table = self.query_one("#table-tasks", DataTable)
        table.clear()
        if not records:
            self.current_key = None
            self._set_detail_label("Preview")
            self._set_detail_text(self._state_text("No tasks", self.module.empty_message, self.module.empty_hint()))
            self.app.update_status("Tasks: no records")
            return
        first_key = records[0].key
        for record in records:
            table.add_row(self._task_cell(record), self._list_cell(record), self._due_cell(record), key=record.key)
        table.move_cursor(row=0, column=0)
        self.show_preview(first_key)
        self.app.update_status(f"Tasks: loaded {len(records)} tasks")

    def _render_tasklists(self) -> None:
        tasklist_list = self.query_one("#tasklist-list-tasks", ListView)
        tasklist_list.clear()
        selected_index = 0
        for index, tasklist in enumerate(self.module.tasklists):
            tasklist_list.append(ListItem(Static(tasklist["name"]), name=tasklist["id"]))
            if tasklist["id"] == self.module.selected_tasklist_id:
                selected_index = index
        if self.module.tasklists:
            tasklist_list.index = selected_index
        self._apply_tasklist_width()

    def _apply_tasklist_width(self) -> None:
        longest = max([len("Task Lists"), *(len(tasklist["name"]) for tasklist in self.module.tasklists)], default=len("Task Lists"))
        width = max(15, longest + 5)
        self.query_one("#pane-tasklists-tasks", Container).styles.width = width

    def _refresh_tasklist_heading(self) -> None:
        self.query_one("#tasklist-heading-tasks", Static).update(self.module.list_label())

    def show_preview(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        record = self.records[key]
        preview = record.preview or f"{record.title}\n{record.subtitle}".strip()
        self._set_detail_label("Preview")
        self._set_detail_text(preview)
        self.app.update_status("Tasks: preview ready, press Enter for full detail")

    def open_record(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        self._set_detail_label("Detail")
        if key in self.detail_cache:
            self._render_detail(self.detail_cache[key])
            return
        self._set_detail_text(self._state_text("Loading Tasks detail", "Fetching the selected task..."))
        self.app.update_status("Loading task detail...")
        self._load_detail(key)

    def _render_detail(self, detail: str) -> None:
        self.query_one("#detail-tasks", Static).update(
            Panel(Text(detail), title=self.module.title, subtitle=self.detail_label, border_style=MODULE_ACCENTS["tasks"], box=box.ROUNDED)
        )
        self.app.update_status("Tasks: detail loaded")

    def _set_detail_text(self, value: str) -> None:
        self.query_one("#detail-tasks", Static).update(
            Panel(Text(value), title=self.module.title, subtitle=self.detail_label, border_style=MODULE_ACCENTS["tasks"], box=box.ROUNDED)
        )

    def _handle_error(self, message: str) -> None:
        self._render_tasklists()
        self._refresh_tasklist_heading()
        table = self.query_one("#table-tasks", DataTable)
        table.clear()
        self.current_key = None
        self._set_detail_label("Preview")
        self._set_detail_text(self._state_text("Tasks request failed", message))
        self.app.update_status("Tasks: request failed")

    def _set_detail_label(self, value: str) -> None:
        self.detail_label = value
        self.query_one("#detail-label-tasks", Static).update(value)

    def _state_text(self, heading: str, message: str, hint: str = "") -> str:
        lines = [heading, "", message]
        if hint:
            lines.extend(["", hint])
        return "\n".join(lines)

    def _task_cell(self, record: Record) -> Text:
        completed = bool(record.raw.get("completed"))
        text = Text()
        text.append("✓ " if completed else "○ ", style="#a3be8c" if completed else "#d08770")
        text.append(record.title, style="#8f959f" if completed else "bold #f2f2f2")
        return text

    def _list_cell(self, record: Record) -> Text:
        text = Text(record.columns[1])
        text.stylize("#8f959f" if record.raw.get("completed") else "#d8dee9")
        return text

    def _due_cell(self, record: Record) -> Text:
        text = Text(record.columns[2])
        text.stylize("#7f848e" if record.raw.get("completed") else "#d8dee9")
        return text

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "table-tasks" or event.row_key is None:
            return
        self.show_preview(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "table-tasks" or event.row_key is None:
            return
        self.open_record(str(event.row_key.value))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "tasklist-list-tasks" or event.item is None:
            return
        tasklist_id = event.item.name or ""
        tasklist = next((item for item in self.module.tasklists if item["id"] == tasklist_id), None)
        if tasklist is None or tasklist_id == self.module.selected_tasklist_id:
            return
        self.module.set_tasklist(tasklist["id"], tasklist["name"])
        self.app.update_status(f"Tasks list: {tasklist['name']}")
        self.action_refresh()

    def current_record(self) -> Record | None:
        if self.current_key is None:
            return None
        return self.records.get(self.current_key)

    def reset_state(self) -> None:
        self.records = {}
        self.current_key = None
        self.detail_cache.clear()
        self.detail_label = "Preview"
        self.loaded = False


class DriveView(ModuleView):
    """Drive-specific list/detail view with folder navigation."""

    module: DriveModule

    def __init__(self, module: DriveModule, client: GwsClient) -> None:
        super().__init__(module, client)

    def open_record(self, key: str) -> None:
        record = self.records.get(key)
        if record is None:
            return
        if record.raw.get("navigate_up"):
            self.module.navigate_up()
            self.current_key = None
            self.app.update_status(f"Drive: {self.module.list_label()}")
            self.action_refresh()
            return
        if record.subtitle == "Folder":
            self.module.enter_folder(record)
            self.current_key = None
            self.app.update_status(f"Drive: {self.module.list_label()}")
            self.action_refresh()
            return
        super().open_record(key)


class Workspace(App):
    """Unified Google Workspace TUI backed by gws."""

    TITLE = "workspace"

    CSS = """
    App {
        background: #1b1b1b;
        color: #f2f2f2;
    }

    Header {
        background: #181818;
        color: #f2f2f2;
    }

    Footer {
        background: #181818;
        color: #bdbdbd;
    }

    #shell {
        height: 1fr;
        padding: 1 2;
        background: #1b1b1b;
    }

    #workspace {
        height: 1fr;
    }

    #sidebar {
        width: 18;
        min-width: 18;
        margin-right: 1;
        padding: 1 0;
        background: #202020;
        border: round #3a3a3a;
    }

    .section-label {
        color: #d0d0d0;
        text-style: bold;
        margin: 0 1 1 1;
    }

    #module-list {
        height: 1fr;
        background: transparent;
        border: tall #343434;
    }

    #content-switcher {
        width: 1fr;
        height: 1fr;
        margin-right: 1;
    }

    #activity-pane {
        width: 28;
        min-width: 28;
        padding: 1 0;
        background: #202020;
        border: round #3a3a3a;
    }

    #activity-label {
        color: #d0d0d0;
        text-style: bold;
        margin: 0 1 1 1;
    }

    #activity-log {
        height: 1fr;
        margin: 0 1;
    }

    .module-frame {
        height: 1fr;
        padding: 1 0;
        background: #202020;
        border: round #3a3a3a;
    }

    .module-title {
        color: #f2f2f2;
        text-style: bold;
        margin-bottom: 0;
    }

    .module-body {
        height: 1fr;
    }

    .module-heading {
        height: auto;
        align: left middle;
        margin: 0 1 1 1;
    }

    #calendar-month-label {
        margin: 0 1 1 1;
    }

    .module-badge {
        width: auto;
        margin-left: 1;
        padding: 0;
        color: #cfcfcf;
        background: transparent;
    }

    .pane {
        height: 1fr;
        padding: 0 1 1 1;
        background: #1b1b1b;
        border: tall #343434;
    }

    .pane-table {
        width: 7fr;
        margin-right: 1;
    }

    .pane-detail {
        width: 5fr;
    }

    .pane-mailboxes {
        width: 3fr;
        margin-right: 1;
    }

    .pane-mail {
        width: 5fr;
        margin-right: 1;
    }

    .pane-mail-detail {
        width: 5fr;
    }

    .pane-title {
        color: #d0d0d0;
        text-style: bold;
        padding: 0 0 1 0;
    }

    #frame-gmail {
        border: round #32464f;
    }

    #frame-calendar {
        border: round #44523a;
    }

    #frame-drive {
        border: round #4b4636;
    }

    #frame-tasks {
        border: round #5b4337;
    }

    #frame-docs {
        border: round #5b5131;
    }

    #title-gmail,
    #frame-gmail .pane-title,
    #badge-gmail {
        color: #88c0d0;
    }

    #title-calendar,
    #frame-calendar .pane-title,
    #badge-calendar {
        color: #a3be8c;
    }

    #title-drive,
    #frame-drive .pane-title,
    #badge-drive {
        color: #d8b56c;
    }

    #title-tasks,
    #frame-tasks .pane-title,
    #badge-tasks {
        color: #d08770;
    }

    #title-docs,
    #frame-docs .pane-title,
    #badge-docs {
        color: #ebcb8b;
    }

    DataTable {
        height: 1fr;
        background: #1b1b1b;
    }

    DataTable > .datatable--header {
        background: #202020;
        color: #d6d6d6;
        text-style: bold;
    }

    DataTable > .datatable--odd-row {
        background: #1b1b1b;
    }

    DataTable > .datatable--even-row {
        background: #1f1f1f;
    }

    DataTable > .datatable--cursor {
        background: #2d3138;
        color: #f7f7f7;
        text-style: bold;
    }

    DataTable > .datatable--header-cursor {
        background: #31353c;
        color: #f7f7f7;
        text-style: bold;
    }

    #calendar-grid {
        height: 1fr;
    }

    .detail-container {
        height: 1fr;
    }

    #mailbox-list-gmail,
    #tasklist-list-tasks {
        height: 1fr;
        background: transparent;
        border: tall #343434;
    }

    ProfilePickerScreen, ComposeEmailScreen, CreateEventScreen, CreateTaskScreen, ConfirmDeleteScreen, DeleteCalendarEventScreen, LabelEditorScreen, GmailSearchScreen, CreateDocumentScreen, EditDocumentScreen, EditSheetScreen {
        align: center middle;
    }

    .modal-window {
        width: 70;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: #202020;
        border: round #3a3a3a;
    }

    #doc-edit-modal, #sheet-edit-modal {
        width: 118;
        height: 88%;
        max-width: 96%;
    }

    .modal-title {
        text-style: bold;
        margin-bottom: 1;
        color: #f2f2f2;
    }

    .modal-subtitle {
        color: #a8a8a8;
        margin-bottom: 1;
    }

    .modal-row {
        height: auto;
        margin-bottom: 1;
    }

    .modal-actions {
        height: auto;
        margin-top: 1;
        align: right middle;
    }

    #compose-modal Input, #event-modal Input, #task-modal Input, #doc-create-modal Input, #gmail-search-modal Input {
        margin-bottom: 1;
    }

    #compose-body, #event-description, #task-notes, #doc-create-body, #doc-edit-body, #sheet-edit-body {
        height: 12;
        margin-bottom: 1;
    }

    #doc-edit-body, #sheet-edit-body {
        height: 1fr;
    }

    #confirm-message {
        margin-bottom: 1;
    }

    #labels-list {
        height: 14;
        border: tall #343434;
        padding: 1;
        margin-bottom: 1;
    }

    #calendar-delete-list {
        height: 12;
        border: tall #343434;
        margin-bottom: 1;
        background: transparent;
    }

    #profile-list {
        height: 12;
        width: 38;
        border: tall #343434;
        margin-bottom: 1;
        background: transparent;
    }

    #profile-modal {
        width: 118;
        max-width: 96%;
    }

    #profile-body {
        height: 16;
        margin-bottom: 1;
    }

    #profile-detail-pane {
        width: 1fr;
        margin-left: 1;
        border: tall #343434;
        padding: 0 1;
        background: #1b1b1b;
    }

    #profile-detail {
        height: 1fr;
    }

    .profile-item {
        height: auto;
        padding: 0 1;
    }

    #labels-empty {
        color: #a8a8a8;
    }

    #status {
        dock: bottom;
        height: 2;
        color: #bdbdbd;
        background: #202020;
        padding: 0 2;
        content-align: left middle;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("r", "regenerate_today", "Regen"),
        ("a", "add_event", "Add"),
        ("c", "compose_email", "Compose"),
        ("d", "delete_email", "Delete"),
        Binding("D", "reject_today_draft", "Reject", key_display="Shift+D"),
        ("e", "reply_email", "Reply"),
        Binding("E", "reply_all_email", "Reply All", key_display="Shift+E"),
        ("f", "forward_email", "Forward"),
        ("l", "edit_labels", "Labels"),
        ("n", "create_doc", "New Doc"),
        ("p", "switch_profile", "Profile"),
        ("/", "search_email", "Search"),
        Binding("A", "approve_today_draft", "Approve", key_display="Shift+A"),
        ("u", "toggle_unread_filter", "Unread"),
        ("x", "toggle_task_complete", "Toggle"),
        ("[", "previous_calendar_month", "Prev Mon"),
        ("]", "next_calendar_month", "Next Mon"),
        ("tab", "next_module", "Next"),
        ("shift+tab", "previous_module", "Prev"),
        ("w", "edit_doc", "Edit"),
    ]

    def __init__(self, client: GwsClient | None = None) -> None:
        super().__init__()
        self.activity_lines: deque[Text] = deque(maxlen=60)
        self.profiles, default_profile_name = discover_profiles()
        self.current_profile_name = default_profile_name
        self.profile_diagnostics_cache: dict[str, GwsProfileDiagnostic] = {}
        self.profile_diagnostics_loaded_at = 0.0
        self.client = client or GwsClient()
        if isinstance(self.client, GwsClient):
            self.client.observer = self._on_gws_event
            if self.current_profile is not None and not self.client.config_dir:
                self.client.config_dir = self.current_profile.config_dir
        self.modules = built_in_modules()
        self.module_views: dict[str, object] = {}
        self.current_module_id = self.modules[0].id
        self._sync_today_profile_name()
        self.prompt_for_profile_on_mount = (
            len(self.profiles) > 1
            and not os.environ.get("GWS_TUI_PROFILE", "").strip()
            and not os.environ.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "").strip()
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="shell"):
            with Horizontal(id="workspace"):
                with PassiveScrollableContainer(id="sidebar"):
                    yield Static("Modules", classes="section-label")
                    yield ListView(
                        *(
                            ListItem(Static(f"{index}. {module.title}"), name=module.id)
                            for index, module in enumerate(self.modules, start=1)
                        ),
                        id="module-list",
                    )
                with ContentSwitcher(initial=f"frame-{self.current_module_id}", id="content-switcher"):
                    for module in self.modules:
                        if isinstance(module, TodayModule):
                            view = TodayView(module, self.client)
                        elif isinstance(module, CalendarModule):
                            view = CalendarGridView(module, self.client)
                        elif isinstance(module, GmailModule):
                            view = GmailView(module, self.client)
                        elif isinstance(module, TasksModule):
                            view = TasksView(module, self.client)
                        elif isinstance(module, DriveModule):
                            view = DriveView(module, self.client)
                        else:
                            view = ModuleView(module, self.client)
                        self.module_views[module.id] = view
                        with Container(id=f"frame-{module.id}", classes="module-frame"):
                            yield view
                with PassiveScrollableContainer(id="activity-pane"):
                    yield Static("gws activity", id="activity-label")
                    yield Static("", id="activity-log")
        yield Static("Ready", id="status")
        yield Footer()

    def on_mount(self) -> None:
        module_list = self.query_one(ListView)
        module_list.index = 0
        self._refresh_profile_label()
        if self.prompt_for_profile_on_mount:
            self._open_profile_picker("Choose Workspace Profile")
            return
        self._show_module(self.current_module_id)

    def on_key(self, event: Key) -> None:
        if not event.character or not event.character.isdigit():
            return
        if len(self.screen_stack) > 1:
            return
        if isinstance(self.focused, Input | TextArea):
            return
        index = int(event.character) - 1
        if not 0 <= index < len(self.modules):
            return
        module_list = self.query_one(ListView)
        module_list.index = index
        self._show_module(self.modules[index].id)
        event.stop()

    def update_status(self, message: str) -> None:
        module = next((item for item in self.modules if item.id == self.current_module_id), None)
        badge = module.badge() if module is not None else "Workspace"
        accent = MODULE_ACCENTS.get(module.id if module is not None else "", "#bdbdbd")
        status = Text()
        status.append(f"[{badge}] ", style=f"bold {accent}")
        status.append(message, style="#c8c8c8")
        self.query_one("#status", Static).update(status)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "refresh":
            return self.current_module_id != "today"
        if action == "regenerate_today":
            return self.current_module_id == "today"
        if action == "add_event":
            return self.current_module_id in {"calendar", "tasks"}
        if action in {"compose_email", "reply_email", "reply_all_email", "forward_email", "edit_labels", "search_email", "toggle_unread_filter"}:
            return self.current_module_id == "gmail"
        if action == "delete_email":
            return self.current_module_id in {"gmail", "calendar"}
        if action == "create_doc":
            return self.current_module_id == "docs"
        if action == "edit_doc":
            return self.current_module_id in {"docs", "sheets"}
        if action == "toggle_task_complete":
            return self.current_module_id == "tasks"
        if action in {"previous_calendar_month", "next_calendar_month"}:
            return self.current_module_id == "calendar"
        if action in {"approve_today_draft", "reject_today_draft"}:
            if self.current_module_id != "today":
                return False
            today_view = self.module_views.get("today")
            if not isinstance(today_view, TodayView):
                return False
            return True if today_view.current_draft_id() else None
        return True

    def _on_gws_event(self, event: GwsCommandEvent) -> None:
        self.call_from_thread(self._record_gws_event, event)

    def _record_gws_event(self, event: GwsCommandEvent) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        command_text = self._format_gws_command(event.command)
        entry = Text()
        entry.append(timestamp, style="#7f848e")
        entry.append("\n")
        entry.append(f"[{event.status}] ", style=f"bold {self._activity_status_color(event.status)}")
        entry.append(command_text, style="#d8dee9")
        if event.detail:
            entry.append(f"\n{event.detail}", style="#a7adba")
        self.activity_lines.appendleft(entry)
        self.query_one("#activity-log", Static).update(Group(*self.activity_lines))

    def _format_gws_command(self, command: list[str]) -> str:
        if len(command) <= 4:
            return " ".join(command)
        if len(command) >= 4 and command[0] == "gws":
            return f"{command[1]} {' '.join(command[2:4])}"
        return " ".join(command[:3])

    def _activity_status_color(self, status: str) -> str:
        if status == "ok":
            return "#a3be8c"
        if status == "error":
            return "#bf616a"
        return "#88c0d0"

    def action_refresh(self) -> None:
        if self.current_module_id == "today":
            self.action_regenerate_today()
            return
        self.module_views[self.current_module_id].action_refresh()

    def action_regenerate_today(self) -> None:
        today_view = self.module_views.get("today")
        if self.current_module_id != "today" or not isinstance(today_view, TodayView):
            self.update_status("Regenerate is only available in Today")
            return
        today_view.action_regenerate()

    def action_approve_today_draft(self) -> None:
        today_module = self._current_today_module()
        today_view = self.module_views.get("today")
        if today_module is None or not isinstance(today_view, TodayView):
            self.update_status("Approve draft is only available in Today")
            return
        draft_id = today_view.current_draft_id()
        if not draft_id:
            self.update_status("Approve draft: select a draft in Today")
            return
        draft = today_module.draft_by_id(draft_id)
        if draft is None:
            self.update_status("Approve draft: draft no longer exists")
            return
        self.update_status(f"Applying draft: {draft.title}")
        self._apply_today_draft(today_module, draft_id)

    def action_reject_today_draft(self) -> None:
        today_module = self._current_today_module()
        today_view = self.module_views.get("today")
        if today_module is None or not isinstance(today_view, TodayView):
            self.update_status("Reject draft is only available in Today")
            return
        draft_id = today_view.current_draft_id()
        if not draft_id:
            self.update_status("Reject draft: select a draft in Today")
            return
        draft = today_module.draft_by_id(draft_id)
        if draft is None:
            self.update_status("Reject draft: draft no longer exists")
            return
        today_module.remove_draft(draft_id)
        today_view.sync_from_module()
        self.update_status(f"Draft rejected: {draft.title}")

    def action_add_event(self) -> None:
        tasks_module = self._current_tasks_module()
        if tasks_module is not None:
            tasklist_id = tasks_module.default_create_tasklist_id()
            tasklist_name = tasks_module.default_create_tasklist_name()
            if not tasklist_id:
                self.update_status("Create task: no writable task list available")
                return
            self.push_screen(CreateTaskScreen(tasklist_id, tasklist_name), self._handle_create_task_result)
            return
        calendar_module = self._current_calendar_module()
        if calendar_module is None:
            self.update_status("Add is only available in Calendar and Tasks")
            return
        default_calendar_id = "primary"
        initial_start = ""
        initial_end = ""
        initial_duration = "60"
        record = self.module_views[self.current_module_id].current_record()
        if record is not None and record.raw.get("calendar_writable") and "calendar_id" in record.raw:
            default_calendar_id = record.raw["calendar_id"]
        calendar_view = self.module_views.get("calendar")
        if isinstance(calendar_view, CalendarGridView):
            selected_day = calendar_view.selected_day()
            if selected_day is not None:
                day_text = selected_day.isoformat()
                initial_start = f"{day_text} 09:00"
        self.push_screen(
            CreateEventScreen(default_calendar_id, initial_start, initial_end, initial_duration),
            self._handle_event_result,
        )

    def action_compose_email(self) -> None:
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Compose is only available in Gmail")
            return
        self.push_screen(ComposeEmailScreen(), self._handle_compose_result)

    def action_search_email(self) -> None:
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Search is only available in Gmail")
            return
        self.push_screen(GmailSearchScreen(gmail_module.search_query), self._handle_search_result)

    def action_toggle_unread_filter(self) -> None:
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Unread filter is only available in Gmail")
            return
        state = gmail_module.toggle_unread_only()
        self.update_status(f"Gmail unread filter {'enabled' if state else 'disabled'}")
        self.module_views["gmail"].action_refresh()

    def action_create_doc(self) -> None:
        docs_module = self._current_docs_module()
        if docs_module is None:
            self.update_status("New doc is only available in Docs")
            return
        self.push_screen(CreateDocumentScreen(), self._handle_create_doc_result)

    def action_toggle_task_complete(self) -> None:
        tasks_module = self._current_tasks_module()
        if tasks_module is None:
            self.update_status("Toggle task is only available in Tasks")
            return
        record = self.module_views[self.current_module_id].current_record()
        if record is None:
            self.update_status("Toggle task: no task selected")
            return
        completed = not bool(record.raw.get("completed"))
        self.update_status("Updating task status...")
        self._toggle_task_status(tasks_module, record, completed)

    def action_switch_profile(self) -> None:
        if len(self.profiles) <= 1:
            self.update_status("Only one workspace profile is available")
            return
        self._open_profile_picker()

    def action_reply_email(self) -> None:
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Reply is only available in Gmail")
            return
        record = self.module_views[self.current_module_id].current_record()
        if record is None:
            self.update_status("Reply: no email selected")
            return
        self.update_status("Loading reply context...")
        self._load_reply_context(gmail_module, record.key)

    def action_reply_all_email(self) -> None:
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Reply all is only available in Gmail")
            return
        record = self.module_views[self.current_module_id].current_record()
        if record is None:
            self.update_status("Reply all: no email selected")
            return
        self.update_status("Loading reply-all context...")
        self._load_reply_all_context(gmail_module, record.key)

    def action_forward_email(self) -> None:
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Forward is only available in Gmail")
            return
        record = self.module_views[self.current_module_id].current_record()
        if record is None:
            self.update_status("Forward: no email selected")
            return
        self.update_status("Loading forward context...")
        self._load_forward_context(gmail_module, record.key)

    def action_edit_doc(self) -> None:
        if self.current_module_id == "docs":
            docs_module = self._current_docs_module()
            if docs_module is None:
                self.update_status("Edit is only available in Docs and Sheets")
                return
            record = self.module_views[self.current_module_id].current_record()
            if record is None:
                self.update_status("Edit doc: no document selected")
                return
            self.update_status("Loading document editor...")
            self._load_doc_editor(docs_module, record)
            return
        if self.current_module_id == "sheets":
            sheets_module = self._current_sheets_module()
            if sheets_module is None:
                self.update_status("Edit is only available in Docs and Sheets")
                return
            record = self.module_views[self.current_module_id].current_record()
            if record is None:
                self.update_status("Edit sheet: no spreadsheet selected")
                return
            self.update_status("Loading sheet editor...")
            self._load_sheet_editor(sheets_module, record)
            return
        self.update_status("Edit is only available in Docs and Sheets")

    def action_delete_email(self) -> None:
        if self.current_module_id == "calendar":
            calendar_module = self._current_calendar_module()
            calendar_view = self.module_views.get("calendar")
            if calendar_module is None or not isinstance(calendar_view, CalendarGridView):
                self.update_status("Delete is only available in Gmail and Calendar")
                return
            selected_day = calendar_view.selected_day()
            records = [record for record in calendar_view.selected_day_records() if record.raw.get("calendar_writable")]
            if selected_day is None:
                self.update_status("Delete event: select a day in the current month")
                return
            if not records:
                self.update_status("Delete event: no writable events on this day")
                return
            self.push_screen(
                DeleteCalendarEventScreen(selected_day.strftime("%A, %B %d"), records),
                lambda key: self._handle_calendar_delete_result(key, {record.key: record for record in records}),
            )
            return
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Delete is only available in Gmail and Calendar")
            return
        record = self.module_views[self.current_module_id].current_record()
        if record is None:
            self.update_status("Trash: no email selected")
            return
        self.push_screen(ConfirmDeleteScreen(record.title or "(No subject)"), lambda confirmed: self._handle_delete_result(confirmed, record.key))

    def action_edit_labels(self) -> None:
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Labels are only available in Gmail")
            return
        record = self.module_views[self.current_module_id].current_record()
        if record is None:
            self.update_status("Labels: no email selected")
            return
        self.update_status("Loading Gmail labels...")
        current_label_ids = set(record.raw.get("label_ids", []))
        self._load_label_editor(gmail_module, record.key, record.title, current_label_ids)

    def action_next_module(self) -> None:
        module_list = self.query_one(ListView)
        if module_list.index is None:
            module_list.index = 0
            return
        next_index = (module_list.index + 1) % len(self.modules)
        module_list.index = next_index
        self._show_module(self.modules[next_index].id)

    def action_previous_calendar_month(self) -> None:
        if self.current_module_id != "calendar":
            return
        view = self.module_views.get("calendar")
        if isinstance(view, CalendarGridView):
            view.action_previous_month()

    def action_next_calendar_month(self) -> None:
        if self.current_module_id != "calendar":
            return
        view = self.module_views.get("calendar")
        if isinstance(view, CalendarGridView):
            view.action_next_month()

    def action_previous_module(self) -> None:
        module_list = self.query_one(ListView)
        if module_list.index is None:
            module_list.index = 0
            return
        next_index = (module_list.index - 1) % len(self.modules)
        module_list.index = next_index
        self._show_module(self.modules[next_index].id)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "module-list" or event.item is None:
            return
        self._show_module(event.item.name or self.current_module_id)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "module-list" or event.item is None:
            return
        self._show_module(event.item.name or self.current_module_id)

    def _show_module(self, module_id: str) -> None:
        self.current_module_id = module_id
        self.query_one(ContentSwitcher).current = f"frame-{module_id}"
        self.refresh_bindings()
        self.module_views[module_id].load_if_needed()

    @property
    def current_profile(self) -> GwsProfile | None:
        if self.current_profile_name is None:
            return None
        return next((profile for profile in self.profiles if profile.name == self.current_profile_name), None)

    def _current_calendar_module(self) -> CalendarModule | None:
        for module in self.modules:
            if module.id == self.current_module_id and isinstance(module, CalendarModule):
                return module
        return None

    def _current_today_module(self) -> TodayModule | None:
        for module in self.modules:
            if module.id == self.current_module_id and isinstance(module, TodayModule):
                return module
        return None

    def _current_gmail_module(self) -> GmailModule | None:
        for module in self.modules:
            if module.id == self.current_module_id and isinstance(module, GmailModule):
                return module
        return None

    def _current_docs_module(self) -> DocsModule | None:
        for module in self.modules:
            if module.id == self.current_module_id and isinstance(module, DocsModule):
                return module
        return None

    def _current_sheets_module(self) -> SheetsModule | None:
        for module in self.modules:
            if module.id == self.current_module_id and isinstance(module, SheetsModule):
                return module
        return None

    def _current_tasks_module(self) -> TasksModule | None:
        for module in self.modules:
            if module.id == self.current_module_id and isinstance(module, TasksModule):
                return module
        return None

    def _open_profile_picker(self, title: str = "Switch Workspace Profile") -> None:
        profiles, default_profile_name = discover_profiles()
        self.profiles = profiles
        if self.current_profile_name is None or not any(profile.name == self.current_profile_name for profile in profiles):
            self.current_profile_name = default_profile_name
        diagnostics = self._profile_diagnostics_snapshot()
        screen = ProfilePickerScreen(self.profiles, diagnostics, self.current_profile_name, title=title)
        self.push_screen(
            screen,
            self._handle_profile_result,
        )
        if self._should_refresh_profile_diagnostics():
            self._load_profile_diagnostics(screen, list(self.profiles))

    def _handle_profile_result(self, profile_name: str | None) -> None:
        if profile_name is None:
            self._ensure_current_module_loaded()
            self.update_status("Profile switch cancelled")
            return
        if profile_name == self.current_profile_name:
            self._ensure_current_module_loaded()
            self.update_status(f"Workspace profile unchanged: {profile_name}")
            return
        profile = next((item for item in self.profiles if item.name == profile_name), None)
        if profile is None:
            self.update_status(f"Unknown workspace profile: {profile_name}")
            return
        self._set_profile(profile)

    def _set_profile(self, profile: GwsProfile) -> None:
        self.current_profile_name = profile.name
        if isinstance(self.client, GwsClient):
            self.client.config_dir = profile.config_dir
        self._sync_today_profile_name()
        self.activity_lines.clear()
        self.query_one("#activity-log", Static).update("")
        self._refresh_profile_label()
        for module in self.modules:
            module.reset_state()
        for view in self.module_views.values():
            view.reset_state()
        self._show_module(self.current_module_id)
        self.update_status(f"Workspace profile: {profile.name}")

    def _refresh_profile_label(self) -> None:
        label = "gws activity"
        if self.current_profile_name:
            label = f"gws activity · {self.current_profile_name}"
        self.query_one("#activity-label", Static).update(label)

    def _sync_today_profile_name(self) -> None:
        for module in self.modules:
            if isinstance(module, TodayModule):
                module.set_profile_name(self.current_profile_name)

    def _profile_diagnostics_snapshot(self) -> dict[str, GwsProfileDiagnostic]:
        diagnostics: dict[str, GwsProfileDiagnostic] = {}
        for profile in self.profiles:
            cached = self.profile_diagnostics_cache.get(profile.name)
            diagnostics[profile.name] = cached or inspect_profile_local(profile)
        return diagnostics

    def _should_refresh_profile_diagnostics(self) -> bool:
        if not self.profile_diagnostics_cache:
            return True
        if any(profile.name not in self.profile_diagnostics_cache for profile in self.profiles):
            return True
        return (time.monotonic() - self.profile_diagnostics_loaded_at) > PROFILE_DIAGNOSTICS_TTL_SECONDS

    def _ensure_current_module_loaded(self) -> None:
        self.module_views[self.current_module_id].load_if_needed()

    @work(thread=True, exclusive=True)
    def _load_profile_diagnostics(self, screen: ProfilePickerScreen, profiles: list[GwsProfile]) -> None:
        for profile in profiles:
            diagnostic = inspect_profile(profile, self.client.binary)
            self.call_from_thread(self._update_profile_diagnostic, screen, diagnostic)
        self.call_from_thread(self._mark_profile_diagnostics_refreshed)

    def _update_profile_diagnostic(self, screen: ProfilePickerScreen, diagnostic: GwsProfileDiagnostic) -> None:
        self.profile_diagnostics_cache[diagnostic.name] = diagnostic
        try:
            if screen in self.screen_stack:
                screen.update_diagnostic(diagnostic)
        except Exception:  # noqa: BLE001
            return

    def _mark_profile_diagnostics_refreshed(self) -> None:
        self.profile_diagnostics_loaded_at = time.monotonic()

    def _handle_event_result(self, result: dict[str, str] | None) -> None:
        if result is None:
            self.update_status("Event creation cancelled")
            return
        calendar_module = self._current_calendar_module()
        if calendar_module is None:
            self.update_status("Add event is only available in Calendar")
            return
        self.update_status("Creating calendar event...")
        self._create_event(
            calendar_module,
            result["calendar_id"],
            result["summary"],
            result["start"],
            result["end"],
            result["duration"],
            result["location"],
            result["description"],
        )

    def _handle_calendar_delete_result(self, event_key: str | None, records: dict[str, Record]) -> None:
        if event_key is None:
            self.update_status("Delete event cancelled")
            return
        calendar_module = self._current_calendar_module()
        record = records.get(event_key)
        if calendar_module is None or record is None:
            self.update_status("Delete event is only available in Calendar")
            return
        self.update_status("Deleting calendar event...")
        self._delete_calendar_event(calendar_module, record)

    def _handle_compose_result(self, result: dict[str, str] | None) -> None:
        if result is None:
            self.update_status("Compose cancelled")
            return
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Compose is only available in Gmail")
            return
        attachment_paths = self._parse_attachment_paths(result.get("attachments", ""))
        if result.get("action") == "draft":
            self.update_status("Saving draft...")
            self._save_draft(
                gmail_module,
                to=result["to"],
                cc=result.get("cc", ""),
                subject=result["subject"],
                body=result["body"],
                attachment_paths=attachment_paths,
            )
            return
        self.update_status("Sending email...")
        self._send_email(
            gmail_module,
            result["to"],
            result["cc"],
            result["subject"],
            result["body"],
            attachment_paths,
        )

    def _handle_search_result(self, result: str | None) -> None:
        if result is None:
            self.update_status("Search cancelled")
            return
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Search is only available in Gmail")
            return
        gmail_module.set_search_query(result)
        self.update_status(f"Gmail scope: {gmail_module.scope_summary()}")
        self.module_views["gmail"].action_refresh()

    def _handle_create_doc_result(self, result: dict[str, str] | None) -> None:
        if result is None:
            self.update_status("Document creation cancelled")
            return
        docs_module = self._current_docs_module()
        if docs_module is None:
            self.update_status("New doc is only available in Docs")
            return
        self.update_status("Creating document...")
        self._create_doc(docs_module, result["title"], result["body"])

    def _handle_create_task_result(self, result: dict[str, str] | None) -> None:
        if result is None:
            self.update_status("Task creation cancelled")
            return
        tasks_module = self._current_tasks_module()
        if tasks_module is None:
            self.update_status("Create task is only available in Tasks")
            return
        self.update_status("Creating task...")
        self._create_task(tasks_module, result["tasklist_id"], result["title"], result["notes"], result["due"])

    def _handle_edit_doc_result(self, result: dict[str, str] | None, record: Record) -> None:
        if result is None:
            self.update_status("Document edit cancelled")
            return
        docs_module = self._current_docs_module()
        if docs_module is None:
            self.update_status("Edit doc is only available in Docs")
            return
        self.update_status("Saving document...")
        self._save_doc(docs_module, record.key, result["body"])

    def _handle_edit_sheet_result(self, result: dict[str, str] | None, context: dict[str, str]) -> None:
        if result is None:
            self.update_status("Sheet edit cancelled")
            return
        sheets_module = self._current_sheets_module()
        if sheets_module is None:
            self.update_status("Edit is only available in Docs and Sheets")
            return
        self.update_status("Saving sheet...")
        self._save_sheet(
            sheets_module,
            spreadsheet_id=context["spreadsheet_id"],
            sheet_title=context["sheet_title"],
            clear_range=context["clear_range"],
            body=result["body"],
        )

    def _handle_reply_result(self, result: dict[str, str] | None, reply_context: dict[str, str]) -> None:
        if result is None:
            self.update_status("Reply cancelled")
            return
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Reply is only available in Gmail")
            return
        attachment_paths = self._parse_attachment_paths(result.get("attachments", ""))
        if result.get("action") == "draft":
            self.update_status("Saving draft...")
            self._save_draft(
                gmail_module,
                to=result["to"],
                cc=result.get("cc", ""),
                subject=result["subject"],
                body=result["body"],
                attachment_paths=attachment_paths,
                thread_id=reply_context["thread_id"],
                in_reply_to=reply_context["in_reply_to"],
                references=reply_context["references"],
            )
            return
        self.update_status("Sending reply...")
        self._send_reply(
            gmail_module,
            to=result["to"],
            cc=result.get("cc", ""),
            subject=result["subject"],
            body=result["body"],
            attachment_paths=attachment_paths,
            thread_id=reply_context["thread_id"],
            in_reply_to=reply_context["in_reply_to"],
            references=reply_context["references"],
        )

    def _parse_attachment_paths(self, value: str) -> list[str]:
        paths: list[str] = []
        for chunk in value.splitlines():
            for part in chunk.split(","):
                cleaned = part.strip()
                if cleaned:
                    paths.append(cleaned)
        return paths

    def _handle_delete_result(self, confirmed: bool, message_id: str) -> None:
        if not confirmed:
            self.update_status("Delete cancelled")
            return
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Trash is only available in Gmail")
            return
        self.update_status("Moving email to trash...")
        self._delete_email(gmail_module, message_id)

    def _handle_label_result(
        self,
        result: list[str] | None,
        message_id: str,
        existing_label_ids: list[str],
    ) -> None:
        if result is None:
            self.update_status("Label edit cancelled")
            return
        if sorted(result) == sorted(existing_label_ids):
            self.update_status("Labels unchanged")
            return
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Labels are only available in Gmail")
            return
        self.update_status("Updating labels...")
        self._update_labels(gmail_module, message_id, existing_label_ids, result)

    @work(thread=True, exclusive=True)
    def _create_event(
        self,
        calendar_module: CalendarModule,
        calendar_id: str,
        summary: str,
        start_text: str,
        end_text: str,
        duration_text: str,
        location: str,
        description: str,
    ) -> None:
        try:
            calendar_module.add_event(
                self.client,
                calendar_id=calendar_id,
                summary=summary,
                start_text=start_text,
                end_text=end_text,
                duration_text=duration_text,
                location=location,
                description=description,
            )
        except ValueError as exc:
            self.call_from_thread(self.update_status, f"Event failed: {exc}")
            return
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Event failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Event failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Calendar event created")
        self.call_from_thread(self.module_views["calendar"].action_refresh)

    @work(thread=True, exclusive=True)
    def _delete_calendar_event(self, calendar_module: CalendarModule, record: Record) -> None:
        try:
            calendar_module.delete_event(
                self.client,
                calendar_id=record.raw["calendar_id"],
                event_id=record.raw["event"]["id"],
            )
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Delete event failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Delete event failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Calendar event deleted")
        self.call_from_thread(self.module_views["calendar"].action_refresh)

    @work(thread=True, exclusive=True)
    def _create_doc(self, docs_module: DocsModule, title: str, body: str) -> None:
        try:
            docs_module.create_document(self.client, title=title, body=body)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Doc create failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Doc create failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Document created")
        self.call_from_thread(self.module_views["docs"].action_refresh)

    @work(thread=True, exclusive=True)
    def _load_doc_editor(self, docs_module: DocsModule, record: Record) -> None:
        try:
            context = docs_module.fetch_editor_context(self.client, record)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Doc editor failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Doc editor failed: {exc}")
            return
        self.call_from_thread(self._show_doc_editor, record, context)

    def _show_doc_editor(self, record: Record, context: dict[str, str]) -> None:
        self.push_screen(
            EditDocumentScreen(context["title"], context["body"]),
            lambda result: self._handle_edit_doc_result(result, record),
        )

    @work(thread=True, exclusive=True)
    def _load_sheet_editor(self, sheets_module: SheetsModule, record: Record) -> None:
        try:
            context = sheets_module.fetch_editor_context(self.client, record)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Sheet editor failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Sheet editor failed: {exc}")
            return
        self.call_from_thread(self._show_sheet_editor, context)

    def _show_sheet_editor(self, context: dict[str, str]) -> None:
        self.push_screen(
            EditSheetScreen(context["title"], context["sheet_title"], context["body"]),
            lambda result: self._handle_edit_sheet_result(result, context),
        )

    @work(thread=True, exclusive=True)
    def _save_doc(self, docs_module: DocsModule, document_id: str, body: str) -> None:
        try:
            docs_module.update_document_text(self.client, document_id=document_id, body=body)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Doc save failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Doc save failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Document saved")
        self.call_from_thread(self.module_views["docs"].action_refresh)

    @work(thread=True, exclusive=True)
    def _create_task(
        self,
        tasks_module: TasksModule,
        tasklist_id: str,
        title: str,
        notes: str,
        due_text: str,
    ) -> None:
        try:
            tasks_module.create_task(self.client, tasklist_id=tasklist_id, title=title, notes=notes, due_text=due_text)
        except ValueError as exc:
            self.call_from_thread(self.update_status, f"Task create failed: {exc}")
            return
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Task create failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Task create failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Task created")
        self.call_from_thread(self.module_views["tasks"].action_refresh)

    @work(thread=True, exclusive=True)
    def _toggle_task_status(self, tasks_module: TasksModule, record: Record, completed: bool) -> None:
        try:
            tasks_module.update_task_status(self.client, record=record, completed=completed)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Task update failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Task update failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Task completed" if completed else "Task reopened")
        self.call_from_thread(self.module_views["tasks"].action_refresh)

    @work(thread=True, exclusive=True)
    def _save_sheet(
        self,
        sheets_module: SheetsModule,
        spreadsheet_id: str,
        sheet_title: str,
        clear_range: str,
        body: str,
    ) -> None:
        try:
            sheets_module.update_sheet_values(
                self.client,
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                clear_range=clear_range,
                body=body,
            )
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Sheet save failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Sheet save failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Sheet saved")
        self.call_from_thread(self.module_views["sheets"].action_refresh)

    @work(thread=True, exclusive=True)
    def _apply_today_draft(self, today_module: TodayModule, draft_id: str) -> None:
        draft = today_module.draft_by_id(draft_id)
        if draft is None:
            self.call_from_thread(self.update_status, "Draft apply failed: draft no longer exists")
            return
        try:
            if draft.kind == "task_create":
                tasks_module = next((module for module in self.modules if isinstance(module, TasksModule)), None)
                if tasks_module is None:
                    raise ValueError("Tasks module unavailable")
                if not tasks_module.default_create_tasklist_id():
                    tasks_module.tasklist_options(self.client)
                tasklist_id = str(draft.payload.get("tasklist_id", "") or tasks_module.default_create_tasklist_id()).strip()
                if not tasklist_id:
                    raise ValueError("No writable task list available")
                title, notes, due_text = task_create_defaults(draft, today_module.current_context)
                if not title:
                    raise ValueError("Task title is required")
                tasks_module.create_task(
                    self.client,
                    tasklist_id=tasklist_id,
                    title=title,
                    notes=notes,
                    due_text=due_text,
                )
                target_module_id = "tasks"
                success_message = "Draft applied: task created"
            elif draft.kind == "calendar_event_create":
                calendar_module = next((module for module in self.modules if isinstance(module, CalendarModule)), None)
                if calendar_module is None:
                    raise ValueError("Calendar module unavailable")
                summary = str(draft.payload.get("summary", "") or draft.title).strip()
                start_text = str(draft.payload.get("start", "") or draft.payload.get("start_text", "")).strip()
                end_text = str(draft.payload.get("end", "") or draft.payload.get("end_text", "")).strip()
                duration_text = str(draft.payload.get("duration", "") or draft.payload.get("duration_text", "")).strip()
                if not summary or not start_text:
                    raise ValueError("Calendar draft requires summary and start time")
                calendar_module.add_event(
                    self.client,
                    calendar_id=str(draft.payload.get("calendar_id", "") or "primary").strip(),
                    summary=summary,
                    start_text=start_text,
                    end_text=end_text,
                    duration_text=duration_text,
                    location=str(draft.payload.get("location", "")).strip(),
                    description=str(draft.payload.get("description", "") or draft.detail).strip(),
                )
                target_module_id = "calendar"
                success_message = "Draft applied: calendar event created"
            elif draft.kind == "doc_create":
                docs_module = next((module for module in self.modules if isinstance(module, DocsModule)), None)
                if docs_module is None:
                    raise ValueError("Docs module unavailable")
                title = str(draft.payload.get("title", "") or draft.title).strip()
                if not title:
                    raise ValueError("Document title is required")
                docs_module.create_document(
                    self.client,
                    title=title,
                    body=str(draft.payload.get("body", "") or draft.detail).strip(),
                )
                target_module_id = "docs"
                success_message = "Draft applied: document created"
            elif draft.kind == "gmail_draft":
                gmail_module = next((module for module in self.modules if isinstance(module, GmailModule)), None)
                if gmail_module is None:
                    raise ValueError("Gmail module unavailable")
                to = str(draft.payload.get("to", "")).strip()
                subject = str(draft.payload.get("subject", "") or draft.title).strip()
                body = str(draft.payload.get("body", "") or draft.detail).strip()
                if not to or not subject or not body:
                    raise ValueError("Gmail draft requires to, subject, and body")
                gmail_module.create_draft(
                    self.client,
                    to=to,
                    cc=str(draft.payload.get("cc", "")).strip(),
                    subject=subject,
                    body=body,
                    attachment_paths=[],
                )
                target_module_id = "gmail"
                success_message = "Draft applied: Gmail draft saved"
            else:
                raise ValueError(f"Unsupported draft type: {draft.kind}")
        except (FileNotFoundError, ValueError) as exc:
            self.call_from_thread(self.update_status, f"Draft apply failed: {exc}")
            return
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Draft apply failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Draft apply failed: {exc}")
            return

        today_module.remove_draft(draft_id)
        self.call_from_thread(self._after_today_draft_applied, target_module_id, success_message)

    def _after_today_draft_applied(self, target_module_id: str, message: str) -> None:
        today_view = self.module_views.get("today")
        if isinstance(today_view, TodayView):
            today_view.sync_from_module()
        target_view = self.module_views.get(target_module_id)
        if target_view is not None and getattr(target_view, "loaded", False):
            target_view.action_refresh()
        self.update_status(message)

    @work(thread=True, exclusive=True)
    def _send_email(
        self,
        gmail_module: GmailModule,
        to: str,
        cc: str,
        subject: str,
        body: str,
        attachment_paths: list[str],
    ) -> None:
        try:
            gmail_module.send_message(
                self.client,
                to=to,
                cc=cc,
                subject=subject,
                body=body,
                attachment_paths=attachment_paths,
            )
        except (FileNotFoundError, ValueError) as exc:
            self.call_from_thread(self.update_status, f"Send failed: {exc}")
            return
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Send failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Send failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Email sent")
        self.call_from_thread(self.module_views["gmail"].action_refresh)

    @work(thread=True, exclusive=True)
    def _save_draft(
        self,
        gmail_module: GmailModule,
        to: str,
        cc: str,
        subject: str,
        body: str,
        attachment_paths: list[str],
        thread_id: str = "",
        in_reply_to: str = "",
        references: str = "",
    ) -> None:
        try:
            gmail_module.create_draft(
                self.client,
                to=to,
                cc=cc,
                subject=subject,
                body=body,
                attachment_paths=attachment_paths,
                thread_id=thread_id,
                in_reply_to=in_reply_to,
                references=references,
            )
        except (FileNotFoundError, ValueError) as exc:
            self.call_from_thread(self.update_status, f"Draft failed: {exc}")
            return
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Draft failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Draft failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Draft saved")
        self.call_from_thread(self.module_views["gmail"].action_refresh)

    @work(thread=True, exclusive=True)
    def _load_reply_context(self, gmail_module: GmailModule, message_id: str) -> None:
        try:
            reply_context = gmail_module.fetch_reply_context(self.client, message_id)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Reply failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Reply failed: {exc}")
            return
        self.call_from_thread(self._show_reply_screen, reply_context)

    def _show_reply_screen(self, reply_context: dict[str, str]) -> None:
        self.push_screen(
            ComposeEmailScreen(
                title="Reply Email",
                subtitle="Send a threaded reply. Optional attachments use local file paths.",
                submit_label="Reply",
                initial_to=reply_context["to"],
                initial_cc=reply_context.get("cc", ""),
                initial_subject=reply_context["subject"],
                initial_body=reply_context["body"],
            ),
            lambda result: self._handle_reply_result(result, reply_context),
        )

    @work(thread=True, exclusive=True)
    def _load_reply_all_context(self, gmail_module: GmailModule, message_id: str) -> None:
        try:
            reply_context = gmail_module.fetch_reply_all_context(self.client, message_id)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Reply all failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Reply all failed: {exc}")
            return
        self.call_from_thread(self._show_reply_all_screen, reply_context)

    def _show_reply_all_screen(self, reply_context: dict[str, str]) -> None:
        self.push_screen(
            ComposeEmailScreen(
                title="Reply All",
                subtitle="Reply to the thread sender and include the other recipients.",
                submit_label="Reply All",
                initial_to=reply_context["to"],
                initial_cc=reply_context.get("cc", ""),
                initial_subject=reply_context["subject"],
                initial_body=reply_context["body"],
            ),
            lambda result: self._handle_reply_result(result, reply_context),
        )

    @work(thread=True, exclusive=True)
    def _load_forward_context(self, gmail_module: GmailModule, message_id: str) -> None:
        try:
            forward_context = gmail_module.fetch_forward_context(self.client, message_id)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Forward failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Forward failed: {exc}")
            return
        self.call_from_thread(self._show_forward_screen, forward_context)

    def _show_forward_screen(self, forward_context: dict[str, str]) -> None:
        self.push_screen(
            ComposeEmailScreen(
                title="Forward Email",
                subtitle="Forward the selected email. Original attachments are not reattached automatically.",
                submit_label="Forward",
                initial_to=forward_context.get("to", ""),
                initial_cc=forward_context.get("cc", ""),
                initial_subject=forward_context["subject"],
                initial_body=forward_context["body"],
            ),
            self._handle_compose_result,
        )

    @work(thread=True, exclusive=True)
    def _send_reply(
        self,
        gmail_module: GmailModule,
        to: str,
        cc: str,
        subject: str,
        body: str,
        attachment_paths: list[str],
        thread_id: str,
        in_reply_to: str,
        references: str,
    ) -> None:
        try:
            gmail_module.reply_to_message(
                self.client,
                to=to,
                cc=cc,
                subject=subject,
                body=body,
                attachment_paths=attachment_paths,
                thread_id=thread_id,
                in_reply_to=in_reply_to,
                references=references,
            )
        except (FileNotFoundError, ValueError) as exc:
            self.call_from_thread(self.update_status, f"Reply failed: {exc}")
            return
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Reply failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Reply failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Reply sent")
        self.call_from_thread(self.module_views["gmail"].action_refresh)

    @work(thread=True, exclusive=True)
    def _load_label_editor(
        self,
        gmail_module: GmailModule,
        message_id: str,
        subject: str,
        current_label_ids: set[str],
    ) -> None:
        try:
            labels = gmail_module.list_user_labels(self.client)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Labels failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Labels failed: {exc}")
            return
        self.call_from_thread(self._show_label_editor, message_id, subject, list(current_label_ids), labels)

    def _show_label_editor(
        self,
        message_id: str,
        subject: str,
        existing_label_ids: list[str],
        labels: list[dict],
    ) -> None:
        self.push_screen(
            LabelEditorScreen(subject, labels, set(existing_label_ids)),
            lambda result: self._handle_label_result(result, message_id, existing_label_ids),
        )

    @work(thread=True, exclusive=True)
    def _update_labels(
        self,
        gmail_module: GmailModule,
        message_id: str,
        existing_label_ids: list[str],
        selected_label_ids: list[str],
    ) -> None:
        try:
            gmail_module.update_message_labels(
                self.client,
                message_id=message_id,
                existing_label_ids=existing_label_ids,
                selected_label_ids=selected_label_ids,
            )
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Labels failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Labels failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Labels updated")
        self.call_from_thread(self.module_views["gmail"].action_refresh)

    @work(thread=True, exclusive=True)
    def _delete_email(self, gmail_module: GmailModule, message_id: str) -> None:
        try:
            gmail_module.trash_message(self.client, message_id=message_id)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Trash failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Trash failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Email moved to trash")
        self.call_from_thread(self.module_views["gmail"].action_refresh)
