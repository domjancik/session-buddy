from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input, Static

from .models import SearchResult
from .resume import ResumeCommand, build_resume_command, prepare_resume_command, run_prepared_resume
from .search import search_sessions
from .text import format_time, truncate


CONTROL_BINDINGS = (
    Binding("ctrl+p", "toggle_preview", "Preview"),
    Binding("ctrl+r", "resume", "Resume"),
    Binding("ctrl+u", "clear_query", "Clear"),
    Binding("escape", "quit", "Quit"),
)


def run_tui(db_path: Path, initial_query: str = "", provider: str | None = None, cwd_filter: str | None = None) -> None:
    app = SessionSearchApp(db_path, initial_query, provider, cwd_filter)
    command = app.run()
    if command is not None:
        prepared = prepare_resume_command(command)
        for warning in prepared.warnings:
            print(warning)
        print(prepared.shell_line())
        run_prepared_resume(prepared)


class SessionSearchApp(App[ResumeCommand | None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #query {
        dock: top;
        height: 3;
        margin: 0 1;
    }

    #status {
        height: 1;
        margin: 0 1;
        color: $text-muted;
    }

    #body {
        height: 1fr;
    }

    #results {
        width: 3fr;
        height: 1fr;
    }

    #preview {
        width: 2fr;
        height: 1fr;
        padding: 0 1;
        border-left: solid $primary;
        overflow-y: auto;
    }
    """

    BINDINGS = list(CONTROL_BINDINGS)

    def __init__(
        self,
        db_path: Path,
        initial_query: str = "",
        provider: str | None = None,
        cwd_filter: str | None = None,
    ) -> None:
        super().__init__()
        self.db_path = db_path
        self.initial_query = initial_query
        self.provider = provider
        self.cwd_filter = cwd_filter
        self.results: list[SearchResult] = []
        self.selected_index = 0
        self.preview_visible = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(value=self.initial_query, placeholder="Search sessions, then press Enter", id="query")
        yield Static("Enter search | Up/Down move | Ctrl-P preview | Ctrl-R resume | Ctrl-U clear | Esc quit", id="status")
        with Horizontal(id="body"):
            yield DataTable(id="results")
            yield Static("", id="preview")
        yield Footer()

    def on_mount(self) -> None:
        table = self.results_table
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("#", "Provider", "Score", "Updated", "Title", "Folder")
        if self.initial_query:
            self.run_search(self.initial_query)
        self.query_one("#query", Input).focus()

    @property
    def results_table(self) -> DataTable:
        return self.query_one("#results", DataTable)

    @property
    def preview_panel(self) -> Static:
        return self.query_one("#preview", Static)

    @property
    def status_panel(self) -> Static:
        return self.query_one("#status", Static)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "query":
            return
        self.run_search(event.value)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.selected_index = max(0, min(event.cursor_row, len(self.results) - 1))
        self.refresh_preview()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.selected_index = max(0, min(event.cursor_row, len(self.results) - 1))
        self.refresh_preview()

    def run_search(self, query: str) -> None:
        query = query.strip()
        if not query:
            self.results = []
            self.selected_index = 0
            self.populate_results()
            self.status_panel.update("Enter search | Up/Down move | Ctrl-P preview | Ctrl-R resume | Ctrl-U clear | Esc quit")
            return
        self.status_panel.update("Searching...")
        self.results = search_sessions(
            self.db_path,
            query,
            limit=50,
            provider=self.provider,
            cwd=self.cwd_filter,
        )
        self.selected_index = 0
        self.populate_results()
        self.status_panel.update(f"{len(self.results)} results for {query!r}")
        self.refresh_preview()

    def populate_results(self) -> None:
        table = self.results_table
        table.clear()
        for index, result in enumerate(self.results, start=1):
            table.add_row(
                str(index),
                result.provider,
                f"{result.score:0.3f}",
                format_time(result.updated_at),
                truncate(result.title, 80),
                truncate(result.cwd, 90),
                key=f"{result.provider}:{result.session_id}",
            )
        if self.results:
            table.move_cursor(row=0, column=0, animate=False)
        self.refresh_preview()

    def refresh_preview(self) -> None:
        panel = self.preview_panel
        panel.display = self.preview_visible
        if not self.preview_visible:
            return
        if not self.results:
            panel.update("No result selected.")
            return
        result = self.results[self.selected_index]
        parts = [
            f"{result.provider}  {result.session_id}",
            result.title,
            result.cwd,
        ]
        if result.git_branch:
            parts.append(f"branch: {result.git_branch}")
        parts.append("")
        parts.extend(result.snippets or ["No snippets available."])
        panel.update("\n".join(parts))

    def action_toggle_preview(self) -> None:
        self.preview_visible = not self.preview_visible
        self.refresh_preview()

    def action_clear_query(self) -> None:
        query = self.query_one("#query", Input)
        query.value = ""
        query.focus()
        self.results = []
        self.selected_index = 0
        self.populate_results()

    def action_resume(self) -> None:
        if not self.results:
            self.status_panel.update("No selected session to resume.")
            return
        result = self.results[self.selected_index]
        self.exit(build_resume_command(result.provider, result.session_id, result.cwd))
