from __future__ import annotations

import calendar as calendar_lib
from collections import deque
from datetime import date
from datetime import datetime

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, ScrollableContainer
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

MODULE_ACCENTS = {
    "gmail": "#88c0d0",
    "calendar": "#a3be8c",
    "drive": "#81a1c1",
    "sheets": "#b8d78d",
    "docs": "#ebcb8b",
}


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

    def __init__(self, calendar_id: str) -> None:
        super().__init__()
        self.calendar_id = calendar_id

    def compose(self) -> ComposeResult:
        with Container(id="event-modal", classes="modal-window"):
            yield Static("Create Calendar Event", classes="modal-title")
            yield Static("Times use local format: YYYY-MM-DD HH:MM", classes="modal-subtitle")
            yield Input(value=self.calendar_id, placeholder="primary", id="event-calendar")
            yield Input(placeholder="Title", id="event-summary")
            with Horizontal(classes="modal-row"):
                yield Input(placeholder="2026-03-06 09:00", id="event-start")
                yield Input(placeholder="2026-03-06 10:00", id="event-end")
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
        if not values["end"]:
            self.app.update_status("Event: end time is required")
            self.query_one("#event-end", Input).focus()
            return
        self.dismiss(values)


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
            with ScrollableContainer(id="labels-list"):
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


class CalendarGridView(ScrollableContainer):
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
                with ScrollableContainer(classes="detail-container"):
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


class ModuleView(ScrollableContainer):
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
                with ScrollableContainer(classes="detail-container"):
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


class GmailView(ScrollableContainer):
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
                with ScrollableContainer(classes="detail-container"):
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
            table.add_row(*record.columns, key=record.key)
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

    #mailbox-list-gmail {
        height: 1fr;
        background: transparent;
        border: tall #343434;
    }

    ComposeEmailScreen, CreateEventScreen, ConfirmDeleteScreen, LabelEditorScreen, GmailSearchScreen, CreateDocumentScreen, EditDocumentScreen {
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

    #compose-modal Input, #event-modal Input, #doc-create-modal Input, #gmail-search-modal Input {
        margin-bottom: 1;
    }

    #compose-body, #event-description, #doc-create-body, #doc-edit-body {
        height: 12;
        margin-bottom: 1;
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
        ("r", "refresh", "Refresh"),
        ("a", "add_event", "Add Event"),
        ("c", "compose_email", "Compose"),
        ("d", "delete_email", "Trash"),
        ("e", "reply_email", "Reply"),
        ("shift+e", "reply_all_email", "Reply All"),
        ("f", "forward_email", "Forward"),
        ("l", "edit_labels", "Labels"),
        ("n", "create_doc", "New Doc"),
        ("/", "search_email", "Search"),
        ("u", "toggle_unread_filter", "Unread"),
        ("[", "previous_calendar_month", "Prev Month"),
        ("]", "next_calendar_month", "Next Month"),
        ("tab", "next_module", "Next"),
        ("shift+tab", "previous_module", "Prev"),
        ("w", "edit_doc", "Edit Doc"),
    ]

    def __init__(self, client: GwsClient | None = None) -> None:
        super().__init__()
        self.activity_lines: deque[Text] = deque(maxlen=60)
        self.client = client or GwsClient()
        if isinstance(self.client, GwsClient):
            self.client.observer = self._on_gws_event
        self.modules = built_in_modules()
        self.module_views: dict[str, ModuleView] = {}
        self.current_module_id = self.modules[0].id

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="shell"):
            with Horizontal(id="workspace"):
                with ScrollableContainer(id="sidebar"):
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
                        if isinstance(module, CalendarModule):
                            view = CalendarGridView(module, self.client)
                        elif isinstance(module, GmailModule):
                            view = GmailView(module, self.client)
                        elif isinstance(module, DriveModule):
                            view = DriveView(module, self.client)
                        else:
                            view = ModuleView(module, self.client)
                        self.module_views[module.id] = view
                        with Container(id=f"frame-{module.id}", classes="module-frame"):
                            yield view
                with ScrollableContainer(id="activity-pane"):
                    yield Static("gws activity", id="activity-label")
                    yield Static("", id="activity-log")
        yield Static("Ready", id="status")
        yield Footer()

    def on_mount(self) -> None:
        module_list = self.query_one(ListView)
        module_list.index = 0
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
        self.module_views[self.current_module_id].action_refresh()

    def action_add_event(self) -> None:
        calendar_module = self._current_calendar_module()
        if calendar_module is None:
            self.update_status("Add event is only available in Calendar")
            return
        default_calendar_id = "primary"
        record = self.module_views[self.current_module_id].current_record()
        if record is not None and "calendar_id" in record.raw:
            default_calendar_id = record.raw["calendar_id"]
        self.push_screen(CreateEventScreen(default_calendar_id), self._handle_event_result)

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
        docs_module = self._current_docs_module()
        if docs_module is None:
            self.update_status("Edit doc is only available in Docs")
            return
        record = self.module_views[self.current_module_id].current_record()
        if record is None:
            self.update_status("Edit doc: no document selected")
            return
        self.update_status("Loading document editor...")
        self._load_doc_editor(docs_module, record)

    def action_delete_email(self) -> None:
        gmail_module = self._current_gmail_module()
        if gmail_module is None:
            self.update_status("Trash is only available in Gmail")
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
        self.module_views[module_id].load_if_needed()

    def _current_calendar_module(self) -> CalendarModule | None:
        for module in self.modules:
            if module.id == self.current_module_id and isinstance(module, CalendarModule):
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
            result["location"],
            result["description"],
        )

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
