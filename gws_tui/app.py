from __future__ import annotations

from rich.panel import Panel
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, ScrollableContainer
from textual.widgets import ContentSwitcher, DataTable, Footer, Header, ListItem, ListView, Static

from gws_tui.client import GwsClient, GwsError
from gws_tui.models import Record
from gws_tui.modules import WorkspaceModule, built_in_modules


class ModuleView(ScrollableContainer):
    """A reusable list/detail view for a workspace module."""

    def __init__(self, module: WorkspaceModule, client: GwsClient) -> None:
        super().__init__(id=f"view-{module.id}")
        self.module = module
        self.client = client
        self.records: dict[str, Record] = {}
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
        record = self.records[key]
        preview = record.preview or f"{record.title}\n{record.subtitle}".strip()
        self.query_one(f"#detail-label-{self.module.id}", Static).update("Preview")
        self._set_detail_text(preview)
        self.app.update_status(f"{self.module.title}: preview ready, press Enter for full detail")

    def open_record(self, key: str) -> None:
        if key not in self.records:
            return
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
                        "Tab / Shift+Tab switch modules\nArrow keys move rows\nEnter loads full detail\nr refresh",
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
