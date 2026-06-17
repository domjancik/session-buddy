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
- Recreating deleted worktrees before resume.

## Interfaces

### CLI

- `session-search index`: scan local session stores and build/update `.session-search/index.sqlite`.
- `session-search status`: report new, changed, deleted, and unchanged source files.
- `session-search search <query>`: print ranked results with provider, score, updated time, title, cwd, id, branch, and snippets.
- `session-search tui [query]`: open the Textual terminal UI.
- `session-search resume <provider> <session_id>`: resume an indexed session.

### TUI

The TUI is implemented with Textual.

- Search input accepts normal text without stealing printable letters.
- Results table shows provider, score, updated time, title, and folder.
- Preview pane shows provider, session id, title, folder, branch, and snippets.
- Index-status banner shows whether the local index is stale.
- Controls:
  - `Enter`: search
  - `Up/Down`: move through results
  - `Ctrl-P`: toggle preview
  - `Ctrl-R`: resume selected session
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

The local index lives at `.session-search/index.sqlite`.

- `sessions`: normalized provider/session metadata and preview text.
- `messages`: parsed transcript messages.
- `message_fts`: SQLite FTS5 table for transcript text.
- `session_fts`: SQLite FTS5 table for title, cwd, prompt, summary, and preview.
- `embeddings`: per-session text chunks and local embedding vectors.
- `source_state`: source file path, mtime, size, and indexed session id for incremental indexing.
- Metadata fingerprints are stored per source so metadata-only changes are detected.

## Freshness Behavior

`session-search status`, `search`, and `tui` compare the current local session stores to the saved `source_state`.

Freshness checks detect:

- New transcript JSONL files.
- Changed transcript file mtime or size.
- Deleted transcript files.
- Claude metadata changes in per-project `sessions-index.json`.
- Codex metadata changes in stable metadata files: `state_5.sqlite` and `session_index.jsonl`.

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

If the indexed cwd no longer exists, resume falls back to the current directory and prints a warning. For Codex, the `-C` argument is rewritten to the fallback directory. For Claude, the process runs from the fallback directory while preserving the original session id.

## Validation

Automated tests cover:

- Claude JSONL and `sessions-index.json` parsing.
- Codex JSONL and `session_index.jsonl` parsing.
- Indexing and search over fixture sessions.
- Resume command construction.
- Stale cwd fallback behavior.
- Real `claude --version` and `codex --version` execution through the resume wrapper.
- Textual TUI bindings and mount smoke test.

Expected verification command:

```sh
uv run --extra test pytest -q
```

## Acceptance Criteria

- `session-search index --semantic-backend hash` indexes local Claude/Codex sessions without parser failures.
- `session-search status` reports stale index state, including metadata-only changes.
- `session-search search "<query>" --auto-index` updates stale sources before returning results.
- `session-search search "<query>"` returns relevant sessions with cwd and snippets.
- `session-search tui` opens a Textual UI, allows normal query typing, and shows an index-status banner.
- `session-search tui --auto-index` updates stale sources before opening the UI.
- `Ctrl-R` in the TUI resumes the selected session or warns clearly when the indexed cwd is stale.
- `session-search resume <provider> <session_id> --print-command` prints the actual prepared command.
- Tests pass locally.
