# Session Search Application Spec

## Summary

Session Search is a local command-line and terminal UI application for finding and resuming Claude and Codex sessions stored on this machine. It indexes session transcripts, metadata, working folders, and snippets into a local SQLite database, then supports full-text, fuzzy, and local semantic-style search across both providers.

The system is private by default: indexing and search run locally, and no session text is sent to an external API.

## Goals

- Search Claude and Codex session history from one interface.
- Show the folder where each session ran.
- Surface useful transcript snippets for matches.
- Resume a selected session through the correct provider CLI.
- Handle stale session folders without crashing.
- Keep repeated indexing fast by skipping unchanged session files.

## Non-Goals

- Hosted search service or shared team index.
- Cloud embeddings or remote LLM calls.
- Browser UI.
- Editing or mutating Claude/Codex session history.
- Automatically recreating deleted worktrees before resume.

## Interfaces

### CLI

- `sb index`: scan local session stores and build/update `~/.session-buddy/index.sqlite`.
- `sb status`: report new, changed, deleted, and unchanged source files.
- `sb search <query>`: print ranked results with provider, score, updated time, title, cwd, id, branch, and snippets.
- `sb tui [query]`: open the Textual terminal UI.
- `sb resume [provider] <session_id>`: resume an indexed session.

### TUI

The TUI is implemented with Textual.

- Search input accepts normal text without stealing printable letters.
- Results table shows provider, score, updated time, title, and folder.
- Preview pane shows provider, session id, title, folder, branch, and snippets.
- Index-status banner shows whether the local index is stale.
- `sb tui` automatically starts a temporary tmux session when tmux is available, the command is outside tmux, and `--no-tmux` was not passed.
- Tmux-pane resume can run a selected resume command in a real tmux split pane.
- Controls:
  - `Enter`: search
  - `Up/Down`: move through results
  - `Ctrl-R`: resume selected session in a tmux split pane
  - `Ctrl-O`: resume selected session externally in the normal terminal
  - `Ctrl-W`: run the inferred `git worktree add` restore command for the selected session when its cwd is missing
  - `Ctrl-P`: toggle preview
  - `Ctrl-U`: clear query
  - `Esc`: quit

## Data Sources

### Claude

- Primary transcript files: `~/.claude/projects/**/*.jsonl`
- Metadata indexes: `~/.claude/projects/**/sessions-index.json`
- Metadata used:
  - `sessionId`
  - `firstPrompt`
  - `summary`
  - `created`
  - `modified`
  - `gitBranch`
  - `projectPath`
  - `originalPath`

### Codex

- Primary transcript files: `~/.codex/sessions/**/*.jsonl`
- Thread metadata: `~/.codex/state_5.sqlite`
- Lightweight session index: `~/.codex/session_index.jsonl`
- Metadata used:
  - `id`
  - `title`
  - `cwd`
  - `created_at_ms`
  - `updated_at_ms`
  - `git_branch`
  - `preview`
  - `thread_name`

## Index Schema

The local index lives at `~/.session-buddy/index.sqlite`.

- `sessions`: normalized provider/session metadata and preview text.
- `messages`: parsed transcript messages.
- `message_fts`: SQLite FTS5 table for transcript text.
- `session_fts`: SQLite FTS5 table for title, cwd, prompt, summary, and preview.
- `embeddings`: per-session text chunks and local embedding vectors.
- `source_state`: source file path, mtime, size, and indexed session id for incremental indexing.
- Metadata fingerprints are stored per source so metadata-only changes are detected.

## Freshness Behavior

`sb status`, `search`, and `tui` compare the current local session stores to the saved `source_state`.

Freshness checks detect:

- New transcript JSONL files.
- Changed transcript file mtime or size.
- Deleted transcript files.
- Claude metadata changes in per-project `sessions-index.json`.
- Codex metadata changes in per-session thread/index rows loaded from `state_5.sqlite` and `session_index.jsonl`.

`search` and `tui` warn when the index is stale. Both support `--auto-index`, which updates changed sources and prunes deleted index entries before running. Auto-index is off by default.

## Search Behavior

Search combines several signals:

- Full text: SQLite FTS5/BM25 over messages and session metadata.
- Fuzzy: Python similarity over title, cwd, first prompt, summary, and preview.
- Semantic-style: local embeddings over chunked session text.
- Recency: small boost for newer sessions.

Default semantic behavior uses deterministic hash embeddings (`hash-v1`) so the tool works without model downloads. Higher-quality local embeddings can be enabled with the `semantic` extra and `--semantic-backend sentence-transformers`.

## Resume Behavior

Resume commands are constructed per provider:

- Codex: `codex resume -C <cwd> <session_id>`
- Claude: `claude --resume <session_id>` from the indexed cwd

Execution resolves actual CLI paths before running:

- `claude`
- `codex`
- common fallback install paths such as `~/.local/bin` and `/opt/homebrew/bin`

If the indexed cwd no longer exists:

- Codex falls back to the current directory and rewrites the `-C` argument.
- Claude does not fall back, because `claude --resume` is project-directory scoped and running from a different cwd can report an indexed session as not found.
- When the missing cwd is under `<repo>/.worktrees/<name>`, resume preparation offers a restore command: `git -C <repo> worktree add <cwd> <branch>`.
- In the TUI, `Ctrl-W` runs that restore command for the selected session and verifies the cwd exists before reporting success.

The TUI exposes two resume modes:

- Tmux pane: starts the prepared resume command in a tmux split pane when `sb tui` is running inside tmux.
- External: exits the TUI and starts the prepared resume command in the normal terminal.

When `sb tui` starts outside tmux and tmux is on `PATH`, the CLI wraps the TUI command in `tmux new-session` with a recursion guard environment variable. `--no-tmux` disables this bootstrap. Tmux-pane mode deliberately delegates terminal emulation, pane sizing, focus, and raw input handling to tmux. The Textual app does not include a dedicated agent input widget and does not try to render the agent session inside a log panel. If tmux is unavailable or the TUI is not running inside tmux, pane resume shows a clear message and the user can use external resume instead.

## Validation

Automated tests cover:

- Claude JSONL and `sessions-index.json` parsing.
- Codex JSONL and `session_index.jsonl` parsing.
- Indexing and search over fixture sessions.
- Resume command construction.
- Stale cwd fallback and restore-command behavior.
- TUI restore-worktree binding and restore runner behavior.
- Real `claude --version` and `codex --version` execution through the resume wrapper.
- Textual TUI bindings and mount smoke test.
- TUI tmux bootstrap command construction and skip conditions.
- Tmux pane command construction and guardrail tests.

Expected verification command:

```sh
uv run --extra test pytest -q
```

## Acceptance Criteria

- `sb index --semantic-backend hash` indexes local Claude/Codex sessions without parser failures.
- `sb status` reports stale index state, including metadata-only changes.
- `sb search "<query>" --auto-index` updates stale sources before returning results.
- `sb search "<query>"` returns relevant sessions with cwd and snippets.
- `sb tui` opens a Textual UI, allows normal query typing, and shows an index-status banner.
- `sb tui` automatically starts inside tmux when tmux is available and the command is not already inside tmux.
- `sb tui --no-tmux` runs the Textual UI directly without tmux bootstrap.
- `sb tui --auto-index` updates stale sources before opening the UI.
- `Ctrl-R` in the TUI resumes the selected session in a tmux split pane or warns clearly when tmux is unavailable, the TUI is not running inside tmux, or the indexed cwd is missing.
- Missing Claude worktree cwd warnings include a concrete `git worktree add` restore command when it can be inferred.
- `Ctrl-W` in the TUI executes the inferred restore command for the selected session's missing worktree.
- `Ctrl-O` in the TUI resumes externally through the normal terminal.
- `sb resume <provider> <session_id> --print-command` prints the actual prepared command.
- `sb resume <provider> <session_id> --tmux-pane` opens the prepared command in a tmux split pane when running inside tmux.
- Tests pass locally.
