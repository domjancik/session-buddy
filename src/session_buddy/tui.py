from __future__ import annotations

import sqlite3
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input, Static

from .database import IndexDatabase
from .models import SearchResult
from .resume import (
    ResumeCommand,
    build_resume_command,
    prepare_resume_command,
    run_prepared_resume,
    run_restore_worktree,
)
from .details import detail_lines, title_origin
from .search import search_sessions
from .text import format_time, truncate
from .tmux import open_tmux_pane


STATUS_HELP = (
    "Enter search | Up/Down move | Ctrl-R tmux pane | Ctrl-O external | "
    "Ctrl-W restore | Ctrl-P preview | Ctrl-U clear | Esc quit"
)

CONTROL_BINDINGS = (
    Binding("ctrl+p", "toggle_preview", "Preview"),
    Binding("ctrl+r", "resume_pane", "Tmux Pane"),
    Binding("ctrl+o", "resume_external", "External"),
    Binding("ctrl+w", "restore_worktree", "Restore"),
    Binding("ctrl+u", "clear_query", "Clear"),
    Binding("escape", "quit", "Quit"),
)


def run_tui(
    db_path: Path,
    initial_query: str = "",
    provider: str | None = None,
    cwd_filter: str | None = None,
    ext: list[str] | None = None,
    branch: str | None = None,
    index_status: str = "",
) -> None:
    # Keywords, not positions: this call silently mis-assigned index_status into a new
    # parameter when the filters were added.
    app = SessionSearchApp(
        db_path,
        initial_query,
        provider=provider,
        cwd_filter=cwd_filter,
        ext=ext,
        branch=branch,
        index_status=index_status,
    )
    command = app.run()
    if command is not None:
        prepared = prepare_resume_command(command)
        for warning in prepared.warnings:
            print(warning)
        if prepared.provider == "claude" and prepared.indexed_cwd_missing:
            print("Cannot resume Claude session until the original cwd is restored.")
            return
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

    #index-status {
        height: 1;
        margin: 0 1;
        color: $warning;
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
        ext: list[str] | None = None,
        branch: str | None = None,
        index_status: str = "",
    ) -> None:
        super().__init__()
        self.db_path = db_path
        self.initial_query = initial_query
        self.provider = provider
        self.cwd_filter = cwd_filter
        self.ext = ext
        self.branch = branch
        self.index_status = index_status
        self.results: list[SearchResult] = []
        self.selected_index = 0
        self.preview_visible = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(value=self.initial_query, placeholder="Search sessions, then press Enter", id="query")
        yield Static(self.index_status, id="index-status")
        yield Static(STATUS_HELP, id="status")
        with Horizontal(id="body"):
            yield DataTable(id="results")
            yield Static("", id="preview")
        yield Footer()

    def on_mount(self) -> None:
        table = self.results_table
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("#", "Provider", "Score", "Updated", "Title", "Name", "Folder")
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
        if event.input.id == "query":
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
            self.status_panel.update(STATUS_HELP)
            return
        self.status_panel.update("Searching...")
        self.results = search_sessions(
            self.db_path,
            query,
            limit=50,
            provider=self.provider,
            cwd=self.cwd_filter,
            ext=self.ext,
            branch=self.branch,
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
                title_origin(result.title_source),
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
        origin = title_origin(result.title_source)
        if origin:
            parts.append(f"title: {origin}")
        if result.git_branch:
            # Labelled `started on` deliberately: the session may have created or pushed
            # others, and the `branches:` line below is the one that shows those.
            parts.append(f"started on: {result.git_branch}")
        parts.extend(self.detail_lines_for(result))
        parts.append("")
        parts.extend(result.snippets or ["No snippets available."])
        panel.update("\n".join(parts))

    def detail_lines_for(self, result: SearchResult) -> list[str]:
        """Branches and annotator metadata, rendered exactly as the CLI renders them."""
        db = IndexDatabase(self.db_path)
        try:
            return detail_lines(db, result.provider, result.session_id)
        except sqlite3.Error:
            return []  # a detail we cannot read must not blank the preview
        finally:
            db.close()

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

    def action_resume_external(self) -> None:
        command = self.selected_resume_command()
        if command is None:
            self.status_panel.update("No selected session to resume.")
            return
        self.exit(command)

    def action_resume_pane(self) -> None:
        command = self.selected_resume_command()
        if command is None:
            self.status_panel.update("No selected session to resume.")
            return
        try:
            prepared = prepare_resume_command(command)
        except RuntimeError as error:
            self.status_panel.update(str(error))
            return
        if prepared.provider == "claude" and prepared.indexed_cwd_missing:
            restore = prepared.restore_worktree_command or "restore the original cwd before resuming"
            self.status_panel.update(f"Claude cwd missing. Restore: {restore}")
            return

        try:
            open_tmux_pane(prepared)
        except RuntimeError as error:
            self.status_panel.update(str(error))
            return

        warning = f" {prepared.warnings[-1]}" if prepared.warnings else ""
        self.status_panel.update(f"Opened tmux pane for {prepared.provider} session.{warning}")

    def action_restore_worktree(self) -> None:
        command = self.selected_resume_command()
        if command is None:
            self.status_panel.update("No selected session to restore.")
            return
        try:
            prepared = prepare_resume_command(command, resolve_executable=False)
        except RuntimeError as error:
            self.status_panel.update(str(error))
            return
        if not prepared.indexed_cwd_missing:
            self.status_panel.update("Selected session cwd already exists.")
            return
        if not prepared.restore_worktree_argv:
            self.status_panel.update("No restore command available for this cwd.")
            return
        self.status_panel.update(f"Restoring worktree: {prepared.restore_worktree_command}")
        try:
            run_restore_worktree(prepared)
        except RuntimeError as error:
            self.status_panel.update(str(error))
            return
        self.status_panel.update("Worktree restored. Press Ctrl-R to resume in a tmux pane or Ctrl-O external.")

    def selected_resume_command(self) -> ResumeCommand | None:
        if not self.results:
            return None
        result = self.results[self.selected_index]
        return build_resume_command(result.provider, result.session_id, result.cwd, result.git_branch)
