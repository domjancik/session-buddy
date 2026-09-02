# Data Model

How Session Buddy represents sessions, and where a new tool plugs in. This file is the
contract: if a change contradicts it, either the change or this file is wrong.

## The two roles

A tool relates to a session in exactly one of two ways. This is the central distinction.

| Role | Owns | Produces | Lives in |
| --- | --- | --- | --- |
| **Provider** | the transcript | session rows | `parsers.py` |
| **Annotator** | metadata *about* someone else's session | namespaced key/values | `annotators/` |

**Provider** — the tool wrote the conversation to disk. Claude Code, Codex, opencode and pi
are providers. A provider is the source of truth for a session's existence, content, cwd, and
timestamps.

**Annotator** — the tool orchestrates, groups, or labels sessions that a provider already
owns. Traycer and Conductor are annotators: they run the providers underneath and record which
*epic* or *workspace* each session belongs to. An annotator never creates a session row.

**A tool can be both.** Omnigent is a meta-harness that orchestrates Claude Code, Codex and
Cursor *and* can run native agents. If those native agents get their own transcripts, Omnigent
gets a parser for them and an annotator for the orchestration metadata it holds about
everything else. The two roles are independent registrations, not a per-tool choice.

Deciding: *does this tool write the transcript?* Yes → provider. No → annotator.

## What is normalized, and what is not

Only fields that every tool means the same way live in the `sessions` table.

**Normalized:** `provider`, `session_id`, `title`, `cwd`, `created_at`, `updated_at`,
`git_branch`, and the transcript-derived text (`first_prompt`, `summary`, `preview`).

Git branches are normalized too, in their own table. Unlike epics and swarms, a branch means
the same thing to every tool, so it belongs in core rather than under an annotator's
namespace.

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
warning. This matters because most installs will have none of these tools present.

Two annotators can claim the same session. Conductor names an individual session ("Review
repo") while Traycer names the agent that ran it, so Conductor carries the higher
`title_priority` — the more specific label wins.

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

### Branches

`sessions.git_branch` is the branch a session *started* on — the providers record it once, at
launch. That is not the branch a session worked on: one that starts on `feat/a`, creates a
worktree for `feat/b` and pushes it leaves `feat/b` nowhere in that column, so filtering on it
silently returns nothing.

`session_branches(provider, session_id, branch, evidence)` holds every branch a session can be
shown to have touched. It is rebuilt from the transcript on each re-index, so it never drifts
from the source, and `evidence` records *how* the branch was seen: `start`, `push`, `worktree`,
`status`, `checkout`.

Which patterns to match is a measurement, not a guess. On a real index, `git push` appears in
186 sessions and `worktree add` in 75, while the obvious `checkout -b` appears in 17 — and had
zero hits in the session that motivated the feature, because branches usually arrive with a new
worktree rather than a checkout in place.

Precision matters more than recall here. The first implementation matched loosely and turned
`-u`, `origin`, `/tmp/wt`, bare shas and ordinary prose into branches; a transcript that merely
*discusses* git manufactured dozens. Every pattern now anchors on a shape git itself emits, and
`normalise` rejects flags, paths, shas, digits and pseudo-refs. `main` is kept: working directly
on it is a fact worth being able to query.

### Harness-injected text

A provider's transcript is not all conversation. pi prepends the full text of any loaded
`SKILL.md` to the user's turn, so left alone it becomes the title, fills the preview, and makes
every skill-using session look alike. `strip_pi_injections` removes those blocks from the
indexed text — not just from the title — because a skill document is not something anyone
searches their own history for. Claude's `<task-notification>` openers are the same phenomenon;
there the fix is an annotator title rather than stripping, since the notification *is* the
turn.

A session left with no text after stripping is not indexed at all: an empty row can never
match, so it is noise rather than a record.

## Reference: the Conductor annotator

Conductor (conductor.build) runs Claude Code and Codex in parallel git worktrees. SQLite at
`~/Library/Application Support/com.conductor.app/conductor.db`:

```sql
sessions(id, agent_type, claude_session_id, title, workspace_id, …)
workspaces(id, repository_id, directory_name, workspace_name, branch, workspace_path, …)
repos(id, name, root_path, …)
```

`sessions.claude_session_id` is our `session_id` and `agent_type` is our `provider`. The column
is named for Claude but holds the id whichever harness ran; `agent_type` disambiguates, so
reading the name literally would drop every Codex session.

Keys written: `workspace_id`, `workspace` (`repo/worktree`), `branch`, `repo`, `workspace_path`,
`session_title`. Groups are workspaces. `title` defaults to the string `Untitled`, which is a
placeholder rather than a name and is not claimed.

## Reference: the Traycer annotator

Traycer stores epics at `~/.traycer/epics/<id>/seeds/*.bin` as Yjs update binaries. The root
map is the epic record defined in `traycerai/traycer` at
`protocol/src/persistence/_internal/epic-schemas.ts`:

```ts
{ id, title, isTitleEditedByUser, createdAt, updatedAt,
  chats, artifacts, deletedArtifacts, tuiAgents, roleClaims }
```

Traycer binds harness sessions in **two** containers within that record, and both must be read:

- `tuiAgents[]` — terminal agents; the binding is `harnessId` + `harnessSessionId` on the entry.
- `chats[]` — desktop-app chats; the binding is `activeSessionChain.{harnessId, sessionId}`.

There is also a **second store**, and it is not a mirror:
`~/.traycer/host/epic-state/<epic_id>/chat/chat.db`, an event-sourced SQLite log. The
materialised chat lives in `chat_projection.projection_json`, in two row kinds whose bindings
live in different places: `tenantKind: tui-agent` under `tuiAgent.{harnessId, harnessSessionId}`,
and `tenantKind: chat` — a desktop chat with no `tuiAgent` — under
`hostPrivate.data.activeSessionChain.{harnessId, sessionId}`. A resumed chat's earlier sessions
survive only in each message's `sessionAnchor`, and are bound from there. It holds no epic title — the epic
is identified by directory name — so the two stores are complementary and the seeds supply the
epic title for a chat store's bindings.

Both normalise to one *binding* (kind, id, title, parent, provider, session_id, workspace).

Keys written: `epic_id`, `epic_title`, `kind`, `parent_id`, `workspace`, plus `agent_id` /
`agent_title` for terminal agents and `chat_id` / `chat_title` for chats.

Three traps worth recording:

- **Reading only `tuiAgents` misses every GUI chat.** An epic driven from the desktop app has
  `tuiAgents: []` and all of its sessions under `chats` — that was a real bug, and the sessions
  it silently skipped were the ones a search most needed titles for.
- `activeSessionChain` names the *live* session only. In the seeds a forked or resumed chat's
  earlier sessions stay un-annotated; in the chat store they are recovered from each message's
  `sessionAnchor` (2 sessions on the machine measured).
- **The same "one container, silently missed" bug recurred one store deeper.** The seeds taught
  that reading only `tuiAgents` loses every GUI chat. chat.db repeats it: GUI chats have no
  `tuiAgent`, and their chain sits under `hostPrivate.data`, so a top-level probe returns 0 and
  looks like proof the store is all terminal agents. It was 15 of 106 rows. When a store has
  container kinds, enumerate them before concluding one is absent.
- `desktop-windows.json` also contains epic names, but only for tabs that are currently open —
  on a real machine that was 10 of 52 epics. It is not a usable source.
- **The two stores disagree about the same epic, and a clean decode is not coverage.** Measured
  on one machine: 197 bound sessions in the seeds, 82 in the chat stores, and *zero* in both.
  One epic's seed decoded fine and reported `tuiAgents: 0, chats: 16` while its chat.db held all
  46 chats and every binding. Absence from one store says nothing about the other. This is why
  `detect()` accepts either store alone, and why the chat stores are read *second*: writes are
  `insert or replace`, so the store Traycer keeps current wins any overlap.
- **Read the chat store with `mode=ro` on the live file, never a copy of the `.db` alone.**
  Traycer runs in WAL mode; a read-only connection follows the `-wal`, but copying the database
  without its sidecar silently returns a stale subset. That mistake is what first made the
  chat store look like it held 52 sessions rather than 82.
- The seed is the only complete local copy of an epic title. Reading it requires an actual Yjs
  decode; pattern-matching the binary gets close enough to look right and is wrong in ways that
  vary per file.

## Non-goals

- **No cross-tool group table.** Epics, swarms and projects are not the same thing.
- **No annotator-created sessions.** If a tool has transcripts, it is a provider.
- **No network.** Every source is local.
