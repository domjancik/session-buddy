# Contributing

Contributions are welcome — bug reports, fixes, and especially **support for new tools**.

## Before anything else: your transcripts are private

This tool indexes your own agent sessions, so its data is whatever you have discussed with a
coding agent: client names, internal tickets, credentials someone pasted once, half-finished
arguments with a colleague. That shapes two rules.

**Never paste raw transcript content into an issue, a PR, or a test fixture.** Redact it, or
invent an equivalent. Every fixture in `tests/` is invented for exactly this reason.

**Nothing leaves the machine.** There are no network calls in `src/`, and there should not be
— no telemetry, no crash reporting, no "anonymous" usage counts. A change that adds one will
be declined regardless of how it is guarded.

## Getting set up

```sh
uv sync --extra test --extra traycer
uv run pytest
```

`uv run sb --db /tmp/scratch.sqlite …` keeps experiments off your real index at
`~/.session-buddy/index.sqlite`. To work against invented data instead of your own history:

```sh
uv run python scripts/demo_index.py /tmp/demo.sqlite
uv run sb --db /tmp/demo.sqlite "retry budget on a dead endpoint" --show-ext
```

## Adding support for a tool

Read [docs/data-model.md](docs/data-model.md) first — it is the contract, and it answers the
only question that decides where your code goes:

> **Does this tool write the transcript?** Yes → it is a **provider**. No → it is an **annotator**.

A provider owns a conversation and produces session rows: Claude Code, Codex, opencode, pi. An
annotator describes sessions someone else owns — Traycer's epics, Conductor's workspaces. A
tool can be both. An annotator must never create a session row; if it has transcripts, it is a
provider.

Practical notes from the four providers and two annotators already here:

- **Find the real store before writing a parser.** Every tool so far has been a surprise:
  opencode is one SQLite database for all sessions, Traycer is Yjs binaries, Conductor names
  its join column `claude_session_id` but stores Codex ids in it too. Guessing a format
  produces code that looks right and is wrong.
- **A shared store breaks two assumptions.** Identity and change detection both assume one file
  per session. See "Providers that share one store" in the data model.
- **Prefer precision to recall in anything heuristic.** The branch extractor first matched
  loosely and turned `-u`, `origin`, `/tmp/wt` and stray prose into branch names. Anchor on
  shapes the tool itself emits.
- **Absence is normal.** Almost nobody has all six tools installed. `detect()` returning False
  must be silent, and a broken store degrades to a warning rather than failing the index.
- **Keep heavy dependencies optional**, behind a `[project.optional-dependencies]` extra, and
  degrade to a warning when it is missing.

## Tests

`uv run pytest` — everything runs offline against fixtures, in about a second.

Two habits worth copying:

- **Never read the machine's real data.** Pass explicit `--*-home` paths (or `annotators=[]`)
  so a test cannot pass because of something in your own history. This has bitten twice.
- **Test the failure, not just the success.** The valuable tests here are the ones for corrupt
  stores, missing dependencies, empty transcripts, and text that merely *looks* like a git
  command.

## Pull requests

Small and focused, with tests. Say what changed and why it changed that way — a note about the
option you rejected is often worth more than the diff.

Conventional commit subjects (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`). No CLA.

If you are unsure whether something fits, open an issue first and ask. That is cheaper than
building it twice.
