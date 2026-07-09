import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input

from session_search.tui import CONTROL_BINDINGS, SessionSearchApp


def test_tui_command_bindings_do_not_use_printable_query_letters() -> None:
    keys = {binding.key for binding in CONTROL_BINDINGS}

    assert {"p", "r", "o", "t", "q", "j", "k", "w"}.isdisjoint(keys)
    assert {"ctrl+p", "ctrl+r", "ctrl+o", "ctrl+w", "ctrl+u", "escape"} <= keys
    assert "ctrl+t" not in keys


def test_textual_app_mounts() -> None:
    async def run() -> None:
        app = SessionSearchApp(Path("unused"))
        async with app.run_test():
            assert app.query_one("#query", Input).placeholder == "Search sessions, then press Enter"
            with pytest.raises(Exception):
                app.query_one("#terminal-input", Input)

    asyncio.run(run())
