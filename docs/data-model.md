# Data Model

How Session Buddy represents sessions, and where a new tool plugs in. This file is the
contract: if a change contradicts it, either the change or this file is wrong.

## The two roles

A tool relates to a session in exactly one of two ways. This is the central distinction.

| Role | Owns | Produces | Lives in |
| --- | --- | --- | --- |
| **Provider** | the transcript | session rows | `parsers.py` |
| **Annotator** | metadata *about* someone else's session | namespaced key/values | `annotators/` |

**Provider** — the tool wrote the conversation to disk. Claude Code, Codex and opencode are
providers. A provider is the source of truth for a session's existence, content, cwd, and
timestamps.

**Annotator** — the tool orchestrates, groups, or labels sessions that a provider already
owns. Traycer is an annotator: it runs the providers underneath and records which *epic* each
session belongs to. An annotator never creates a session row.

**A tool can be both.** Omnigent is a meta-harness that orchestrates Claude Code, Codex and
Cursor *and* can run native agents. If those native agents get their own transcripts, Omnigent
gets a parser for them and an annotator for the orchestration metadata it holds about
everything else. The two roles are independent registrations, not a per-tool choice.

Deciding: *does this tool write the transcript?* Yes → provider. No → annotator.

## What is normalized, and what is not

Only fields that every tool means the same way live in the `sessions` table.

**Normalized:** `provider`, `session_id`, `title`, `cwd`, `created_at`, `updated_at`,
`git_branch`, and the transcript-derived text (`first_prompt`, `summary`, `preview`).

**Not normalized:** everything an orchestrator knows. Traycer has epics; Omnigent has swarms;
a future tool will have projects or runs. These look alike and are not: flattening them into
one `group_id` column forces a lossy mapping and destroys the tool's own semantics. They stay
namespaced under the annotator's `source`.

**`title` is the one exception.** It is the only field a person would rank or scan on
regardless of which tool produced it, so an annotator may claim it. This is not cosmetic:
sessions orchestrated by another tool routinely open with machine chatter, so the
provider-derived title reads `<task-notification> <task-id>bi8qdvx1g…` while the orchestrator
holds `ACME-142 retry backoff gate`. Promoting that title is most of the value of annotation.

## Schema

```sql
sessions(provider, session_id, title, provider_title, title_source, cwd, …)
annotations(provider, session_id, source, key, value,
            primary key (provider, session_id, source, key))
```

- `provider_title` — what the parser derived, kept verbatim.
- `title` — the effective title: an annotator's claim if one won, else `provider_title`.
- `title_source` — who won (`provider`, or the annotator's `source`). Without this, a title
  that disagrees with the transcript is undebuggable, and with two annotators installed you
  cannot tell which one applied.

### Title resolution

Highest `title_priority` wins; ties break on `source` name so the result is deterministic and
does not depend on registration order. Resolution runs as one pass over all claims, and first
resets every session to `provider_title` — so when an annotator stops claiming a title (epic
deleted, tool uninstalled) the session reverts instead of keeping a stale name.

### Groups

An annotator also reports its containers (Traycer epics, and whatever a later tool calls
them) via `AnnotatorResult.groups`. They are stored in `annotation_groups` at index time and
read back by `sb groups`, so the command honours `--db` like every other subcommand and works
with the orchestrator absent.

Each group names the annotation key that points back at it (`member_key`, `epic_id` for
Traycer), which is what lets the count be derived generically: the number of *indexed*
sessions carrying that key/value. That is deliberately not the annotator's own tally — the
count should describe what a search can reach, so a group whose sessions were never indexed
reads 0.

### Annotation lifecycle

`replace_annotations(source, …)` is wholesale per source: a source's rows are deleted and
rewritten together, so removals propagate. Sources never see each other's rows.

Annotations live in their own table rather than as session columns, which means re-indexing a
transcript never drops them, and an annotator never has to read transcripts.

## Indexing order

1. Providers parse transcripts and upsert session rows.
2. Annotators run last, keyed on `(provider, session_id)` — those rows must already exist.
3. Titles resolve in a single pass across every annotator's claims.

Each annotator is isolated. One that raises contributes nothing and records a warning; it never
fails the index. Absence of a tool is normal and silent — `detect()` returning `False` is not a
warning. This matters because most installs will have neither Traycer nor Omnigent present.

## Adding an annotator

Add `annotators/<tool>.py` exposing `source`, `title_priority`, `detect(home)` and
`collect(home) -> AnnotatorResult`, then register it in `annotators/__init__.py`.

- `detect()` must not raise. Absence is the common case.
- `collect()` reports failure via `AnnotatorResult.warning` rather than raising.
- Read only the tool's own store. Annotators must not parse transcripts.
- Namespace every key under your `source`. Do not add columns to `sessions`.
- Heavy or fragile dependencies go in a `[project.optional-dependencies]` extra, and
  `collect()` degrades to a warning when the extra is missing.

## Adding a provider

Add parsing to `parsers.py` (`scan_*`, `parse_*` returning `SessionRecord`) and call it from
`index_all`. A provider owns session identity, so its `session_id` must match whatever an
orchestrator would reference — that identifier is the join key annotators depend on.

### Providers that share one store

Claude and Codex give each session its own file, so `source_path` is that file and freshness
compares its `(mtime, size)`. opencode keeps every session in one SQLite database, which breaks
both assumptions:

- **Identity** — `source_path` is synthesised as `<db>#<session_id>`. It is never opened as a
  file; it only has to be unique per session.
- **Change detection** — the database's mtime moves whenever *any* session is written, so
  stat'ing it marks every session stale on every write. Pass `stat_key=` to `should_skip` and
  `update_freshness_for_path` with a per-session value instead (opencode uses
  `(time_updated, time_created)`).

Any future provider backed by a shared database or a server should follow the same two rules.

## Reference: the Traycer annotator

Traycer stores epics at `~/.traycer/epics/<id>/seeds/*.bin` as Yjs update binaries. The root
map is the epic record defined in `traycerai/traycer` at
`protocol/src/persistence/_internal/epic-schemas.ts`:

```ts
{ id, title, isTitleEditedByUser, createdAt, updatedAt,
  chats, artifacts, deletedArtifacts, tuiAgents, roleClaims }
```

Traycer binds harness sessions in **two** containers, and both must be read:

- `tuiAgents[]` — terminal agents; the binding is `harnessId` + `harnessSessionId` on the entry.
- `chats[]` — desktop-app chats; the binding is `activeSessionChain.{harnessId, sessionId}`.

Both normalise to one *binding* (kind, id, title, parent, provider, session_id, workspace).

Keys written: `epic_id`, `epic_title`, `kind`, `parent_id`, `workspace`, plus `agent_id` /
`agent_title` for terminal agents and `chat_id` / `chat_title` for chats.

Three traps worth recording:

- **Reading only `tuiAgents` misses every GUI chat.** An epic driven from the desktop app has
  `tuiAgents: []` and all of its sessions under `chats` — that was a real bug, and the sessions
  it silently skipped were the ones a search most needed titles for.
- `activeSessionChain` names the *live* session only. A chat that was forked or resumed no
  longer references its earlier sessions, so those stay un-annotated.
- `desktop-windows.json` also contains epic names, but only for tabs that are currently open —
  on a real machine that was 10 of 52 epics. It is not a usable source.
- The seed is the only complete local copy of an epic title. Reading it requires an actual Yjs
  decode; pattern-matching the binary gets close enough to look right and is wrong in ways that
  vary per file.

## Non-goals

- **No cross-tool group table.** Epics, swarms and projects are not the same thing.
- **No annotator-created sessions.** If a tool has transcripts, it is a provider.
- **No network.** Every source is local.
