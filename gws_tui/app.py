from __future__ import annotations

from rich.panel import Panel
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, DataTable, Footer, Header, Input, ListItem, ListView, Static, TextArea

from gws_tui.client import GwsClient, GwsError
from gws_tui.models import Record
from gws_tui.modules import WorkspaceModule, built_in_modules
from gws_tui.modules.calendar import CalendarModule
from gws_tui.modules.gmail import GmailModule


class ComposeEmailScreen(ModalScreen[dict[str, str] | None]):
    """Compose a plain text email."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Container(id="compose-modal", classes="modal-window"):
            yield Static("Compose Email", classes="modal-title")
            yield Static("Send a plain text Gmail message.", classes="modal-subtitle")
            yield Input(placeholder="recipient@example.com", id="compose-to")
            yield Input(placeholder="Subject", id="compose-subject")
            yield TextArea("", id="compose-body")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="compose-cancel")
                yield Button("Send", variant="primary", id="compose-send")

    def on_mount(self) -> None:
        self.query_one("#compose-to", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "compose-cancel":
            self.dismiss(None)
            return
        if event.button.id != "compose-send":
            return
        to = self.query_one("#compose-to", Input).value.strip()
        subject = self.query_one("#compose-subject", Input).value.strip()
        body = self.query_one("#compose-body", TextArea).text.strip()
        if not to:
            self.app.update_status("Compose: recipient is required")
            self.query_one("#compose-to", Input).focus()
            return
        if not subject:
            self.app.update_status("Compose: subject is required")
            self.query_one("#compose-subject", Input).focus()
            return
        if not body:
            self.app.update_status("Compose: body is required")
            self.query_one("#compose-body", TextArea).focus()
            return
        self.dismiss({"to": to, "subject": subject, "body": body})


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


class ModuleView(ScrollableContainer):
    """A reusable list/detail view for a workspace module."""

    def __init__(self, module: WorkspaceModule, client: GwsClient) -> None:
        super().__init__(id=f"view-{module.id}")
        self.module = module
        self.client = client
        self.records: dict[str, Record] = {}
        self.current_key: str | None = None
        self.detail_cache: dict[str, str] = {}
        self.loaded = False

    def compose(self) -> ComposeResult:
        yield Static(self.module.title, classes="module-title")
        yield Static(self.module.description, classes="module-description")
        with Horizontal(classes="module-body"):
            with Container(classes="pane pane-table"):
                yield Static("Results", classes="pane-title")
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

    def load_if_needed(self) -> None:
        if not self.loaded:
            self.action_refresh()

    def action_refresh(self) -> None:
        self.loaded = True
        self.detail_cache.clear()
        self.query_one(f"#detail-label-{self.module.id}", Static).update("Preview")
        self._set_detail_text("Loading data...")
        self.app.update_status(f"Loading {self.module.title.lower()}...")
        self._load_records()

    def show_preview(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        record = self.records[key]
        preview = record.preview or f"{record.title}\n{record.subtitle}".strip()
        self.query_one(f"#detail-label-{self.module.id}", Static).update("Preview")
        self._set_detail_text(preview)
        self.app.update_status(f"{self.module.title}: preview ready, press Enter for full detail")

    def open_record(self, key: str) -> None:
        if key not in self.records:
            return
        self.current_key = key
        self.query_one(f"#detail-label-{self.module.id}", Static).update("Detail")
        if key in self.detail_cache:
            self._render_detail(self.detail_cache[key])
            return
        self._set_detail_text("Loading full detail...")
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
            self.app.call_from_thread(self._set_detail_text, f"Error: {exc.message}")
            self.app.call_from_thread(self.app.update_status, f"{self.module.title} request failed")
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._set_detail_text, f"Error: {exc}")
            self.app.call_from_thread(self.app.update_status, f"{self.module.title} detail failed")
            return
        self.detail_cache[key] = detail
        self.app.call_from_thread(self._render_detail, detail)

    def _render_records(self, records: list[Record]) -> None:
        self.records = {record.key: record for record in records}
        table = self.query_one(DataTable)
        table.clear()
        if not records:
            self.query_one(f"#detail-label-{self.module.id}", Static).update("Preview")
            self._set_detail_text(self.module.empty_message)
            self.app.update_status(f"{self.module.title}: no records")
            return

        first_key = records[0].key
        for record in records:
            table.add_row(*record.columns, key=record.key)
        table.move_cursor(row=0, column=0)
        self.show_preview(first_key)
        self.app.update_status(f"{self.module.title}: loaded {len(records)} records")

    def _render_detail(self, detail: str) -> None:
        self.query_one(f"#detail-{self.module.id}", Static).update(Panel(Text(detail), title=self.module.title))
        self.app.update_status(f"{self.module.title}: detail loaded")

    def _set_detail_text(self, value: str) -> None:
        self.query_one(f"#detail-{self.module.id}", Static).update(Panel(Text(value), title=self.module.title))

    def _handle_error(self, message: str) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self.query_one(f"#detail-label-{self.module.id}", Static).update("Preview")
        self._set_detail_text(f"Error: {message}")
        self.app.update_status(f"{self.module.title}: request failed")

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


class WorkspaceApp(App):
    """Unified Google Workspace TUI backed by gws."""

    CSS = """
    App {
        background: #1b1b1b;
        color: #f2f2f2;
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
        width: 30;
        min-width: 30;
        margin-right: 1;
        padding: 1 1 0 1;
        background: #202020;
        border: round #3a3a3a;
    }

    #brand {
        text-style: bold;
        color: #f2f2f2;
        margin-bottom: 1;
    }

    .section-label {
        color: #d0d0d0;
        text-style: bold;
        margin-bottom: 1;
    }

    #module-list {
        height: auto;
        background: transparent;
        border: tall #343434;
        margin-bottom: 1;
    }

    #sidebar-help {
        color: #a8a8a8;
    }

    #content-switcher {
        width: 1fr;
    }

    .module-frame {
        height: 1fr;
        padding: 1;
        background: #202020;
        border: round #3a3a3a;
    }

    .module-title {
        color: #f2f2f2;
        text-style: bold;
        margin-bottom: 1;
    }

    .module-description {
        color: #aaaaaa;
        margin-bottom: 1;
    }

    .module-body {
        height: 1fr;
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

    .pane-title {
        color: #d0d0d0;
        text-style: bold;
        padding: 0 0 1 0;
    }

    DataTable {
        height: 1fr;
        background: #1b1b1b;
    }

    .detail-container {
        height: 1fr;
    }

    ComposeEmailScreen, CreateEventScreen, ConfirmDeleteScreen {
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

    #compose-modal Input, #event-modal Input {
        margin-bottom: 1;
    }

    #compose-body, #event-description {
        height: 12;
        margin-bottom: 1;
    }

    #confirm-message {
        margin-bottom: 1;
    }

    #status {
        dock: bottom;
        height: 1;
        color: #bdbdbd;
        background: #202020;
        padding: 0 2;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("a", "add_event", "Add Event"),
        ("c", "compose_email", "Compose"),
        ("d", "delete_email", "Trash"),
        ("tab", "next_module", "Next"),
        ("shift+tab", "previous_module", "Prev"),
    ]

    def __init__(self, client: GwsClient | None = None) -> None:
        super().__init__()
        self.client = client or GwsClient()
        self.modules = built_in_modules()
        self.module_views: dict[str, ModuleView] = {}
        self.current_module_id = self.modules[0].id

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="shell"):
            with Horizontal(id="workspace"):
                with ScrollableContainer(id="sidebar"):
                    yield Static("gws workspace", id="brand")
                    yield Static("Modules", classes="section-label")
                    yield ListView(
                        *(ListItem(Static(module.title), name=module.id) for module in self.modules),
                        id="module-list",
                    )
                    yield Static(
                        "Tab / Shift+Tab switch modules\nArrow keys move rows\nEnter loads full detail\na add calendar event\nc compose email\nd move to trash\nr refresh",
                        id="sidebar-help",
                    )
                with ContentSwitcher(initial=f"frame-{self.current_module_id}", id="content-switcher"):
                    for module in self.modules:
                        view = ModuleView(module, self.client)
                        self.module_views[module.id] = view
                        with Container(id=f"frame-{module.id}", classes="module-frame"):
                            yield view
        yield Static("Ready", id="status")
        yield Footer()

    def on_mount(self) -> None:
        module_list = self.query_one(ListView)
        module_list.index = 0
        self._show_module(self.current_module_id)

    def update_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

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

    def action_next_module(self) -> None:
        module_list = self.query_one(ListView)
        if module_list.index is None:
            module_list.index = 0
            return
        next_index = (module_list.index + 1) % len(self.modules)
        module_list.index = next_index
        self._show_module(self.modules[next_index].id)

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
        self.update_status("Sending email...")
        self._send_email(gmail_module, result["to"], result["subject"], result["body"])

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
    def _send_email(self, gmail_module: GmailModule, to: str, subject: str, body: str) -> None:
        try:
            gmail_module.send_message(self.client, to=to, subject=subject, body=body)
        except GwsError as exc:
            self.call_from_thread(self.update_status, f"Send failed: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.update_status, f"Send failed: {exc}")
            return
        self.call_from_thread(self.update_status, "Email sent")
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
