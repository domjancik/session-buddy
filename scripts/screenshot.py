"""Render a TUI screenshot (SVG) from the demo index.

Runs the real app headlessly against invented data, so the image in the README always
matches what the code actually draws:

    uv run python scripts/demo_index.py /tmp/sb-demo.sqlite
    uv run python scripts/screenshot.py /tmp/sb-demo.sqlite docs/tui.svg
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from session_buddy.tui import SessionSearchApp  # noqa: E402

QUERY = "retry delivery webhook queue"
SIZE = (124, 20)


def strip_chrome(svg: str) -> str:
    """Drop the fake window frame Rich draws, keeping only the terminal content.

    Rich renders a macOS-style title bar: three circles and the app name, above a content
    group at translate(9, 41). Side padding is 9, so the bar is 32px tall — remove it, pull
    the content up to a symmetric top padding, and shorten the canvas by the same amount.
    """
    bar = 32
    svg = re.sub(r"<g transform=\"translate\(26,22\)\">.*?</g>", "", svg, flags=re.S)
    svg = re.sub(r"<text[^>]*-title[^>]*>.*?</text>", "", svg, flags=re.S)
    svg = svg.replace("<g transform=\"translate(9, 41)\"", "<g transform=\"translate(9, 9)\"")

    def shrink_viewbox(match: re.Match[str]) -> str:
        x, y, w, h = match.group(1).split()
        return f'viewBox="{x} {y} {w} {float(h) - bar}"'

    svg = re.sub(r'viewBox="([^"]+)"', shrink_viewbox, svg, count=1)
    return re.sub(
        r'(<rect fill="[^"]*" stroke="[^"]*" stroke-width="1" x="1" y="1" width="[\d.]+" height=")([\d.]+)(")',
        lambda m: f"{m.group(1)}{float(m.group(2)) - bar}{m.group(3)}",
        svg,
        count=1,
    )


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
        out_path.write_text(strip_chrome(app.export_screenshot()))
    print(f"screenshot written: {out_path}")


if __name__ == "__main__":
    db = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sb-demo.sqlite")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/tui.svg")
    asyncio.run(capture(db, out))
