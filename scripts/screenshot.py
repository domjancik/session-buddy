"""Render a TUI screenshot (SVG) from the demo index.

Runs the real app headlessly against invented data, so the image in the README always
matches what the code actually draws:

    uv run python scripts/demo_index.py /tmp/sb-demo.sqlite
    uv run python scripts/screenshot.py /tmp/sb-demo.sqlite docs/tui.svg
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from session_buddy.tui import SessionSearchApp  # noqa: E402

QUERY = "retry delivery webhook queue"
SIZE = (124, 20)


async def capture(db_path: Path, out_path: Path) -> None:
    app = SessionSearchApp(
        db_path,
        QUERY,
        provider=None,
        cwd_filter=None,
        index_status="Index up to date (8 indexed sources checked).",
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("enter")          # run the query
        await pilot.pause()
        await pilot.press("down")           # highlight a result so the preview fills
        await pilot.pause()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        app.save_screenshot(str(out_path))
    print(f"screenshot written: {out_path}")


if __name__ == "__main__":
    db = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sb-demo.sqlite")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/tui.svg")
    asyncio.run(capture(db, out))
