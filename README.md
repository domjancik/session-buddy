# Session Search

Local full-text, fuzzy, and semantic search over Claude and Codex session history.

## Install

```sh
uv sync --extra test
```

For higher-quality local semantic embeddings, install the optional model stack:

```sh
uv sync --python 3.12 --extra test --extra semantic
uv run session-search index --force --semantic-backend sentence-transformers
```

Without the semantic extra, the tool still provides local semantic-style matching with deterministic hashed embeddings.

## Index Sessions

```sh
uv run session-search index
```

By default this scans:

- `~/.claude/projects`
- `~/.codex/sessions`
- `~/.codex/state_5.sqlite`
- `~/.codex/session_index.jsonl`

The index is stored at `.session-search/index.sqlite`.

## Index Status

```sh
uv run session-search status
```

Status reports new, changed, deleted, and unchanged session sources. A source is considered changed when its transcript file changes or when provider metadata changes, such as Claude `sessions-index.json` or Codex per-session thread/index metadata.

## Search

```sh
uv run session-search search "webhook retry"
uv run session-search search "acme-mono" --provider codex --limit 20
uv run session-search search "routing" --auto-index
```

Search warns when the index is stale. Pass `--auto-index` to update changed sources and prune deleted index entries before searching.

## TUI

The terminal UI is built with Textual.

```sh
uv run session-search tui
uv run session-search tui --auto-index
```

The TUI shows an index-status banner on startup. Pass `--auto-index` to update changed sources and prune deleted index entries before opening the interface.

Keys:

- Type a query and press `Enter` to search.
- Use arrow keys to move through results.
- Press `Ctrl-R` to resume the selected session in the embedded terminal pane.
- Press `Ctrl-O` to resume the selected session externally in the normal terminal.
- Press `Ctrl-P` to toggle preview.
- Press `Ctrl-T` to stop the embedded terminal process.
- Press `Ctrl-U` to clear the query.
- Press `Esc` to quit.

The embedded terminal pane is PTY-backed and works well for line-oriented interaction. Full-screen nested TUIs may be limited by Textual’s lack of a native terminal-emulator widget.

## Resume

```sh
uv run session-search resume codex 019eabd2-9955-77e0-8fd8-2927fbbd3cff
uv run session-search resume claude 8a4837df-fda2-4e32-b612-2dacc03d8698
```

Codex resumes with `codex resume -C <cwd> <session_id>`.
Claude resumes with `claude --resume <session_id>` from the indexed session folder.
If an indexed folder no longer exists, resume falls back to the current directory and prints a warning.
