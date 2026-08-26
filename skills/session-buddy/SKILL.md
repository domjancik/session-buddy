---
name: session-buddy
description: Search and resume past coding-agent sessions (Claude Code, Codex, opencode, pi) by content. Use whenever the answer might already exist in an earlier session — "find the session that...", "which agent did X", "did we already investigate Y", "resume the session about Z", "what was the conclusion on <ticket>" — or when the user references prior work you have no context for. Use it before re-deriving an investigation from scratch. Searches transcripts across every repo, worktree, and every provider; grepping the raw transcript directories is the inferior fallback.
---

# Session Buddy

Local full-text + semantic search over Claude Code, Codex, opencode and pi session transcripts, with resume.

The CLI is `sb` (long form: `session-buddy`). If `command -v sb` finds nothing,
say it is not installed rather than improvising a grep pipeline — point the user at the repo.

## Search

A bare query means search. The index lives at `~/.session-buddy/index.sqlite` regardless of cwd.

```sh
sb "retry backoff PR review" --limit 8
```

| Flag | Use |
| --- | --- |
| `--limit N` | default 20; 6–10 keeps agent context small |
| `--provider claude\|codex\|opencode\|pi` | narrow when you know which agent ran it |
| `--cwd <substring>` | scope to one repo across all its worktrees, e.g. `--cwd acme-mono` |
| `--auto-index` | refresh stale sources first; a full pass takes ~1 min |
| `--no-semantic` | literal matching only |
| `--ext SOURCE[.KEY][=VALUE]` | filter on orchestrator metadata, e.g. `--ext traycer.epic_title=Payments` |
| `--show-ext` | print that metadata under each hit |
| `--branch NAME` | sessions that touched a branch (substring or `*` glob), not just started on it |

Orchestrators (Traycer and Conductor today) may annotate sessions they ran. When present, titles come from
the orchestrator rather than the transcript's first line, and `sb groups` lists the epics with
id, session count, and title. Use `--ext traycer` or `--ext conductor` to restrict to
orchestrated sessions, `--ext conductor.workspace=<repo/worktree>` to scope to one worktree, and
`--show-ext` when the user asks which epic or agent a session belonged to. None of this exists
on a machine without the tool — treat its absence as normal, not as an error.

Results carry relevance score, session id, cwd, git branch, last-updated time, and inline
matched excerpts. Read the excerpts first — they often already contain the answer, so no
transcript needs opening.

Use `sb search <word>` explicitly when the query is a single word that collides with a
subcommand name (`index`, `search`, `tui`, `status`, `resume`).

## Query writing

Semantic matching rewards description, not identifiers alone.

- Good: `"why webhooks were not retried after a failed delivery"`
- Good: `"ACME-142 retry backoff rollout verdict"` — ticket id plus what happened
- Weak: `"2453"` — a bare number matches unrelated hashes, line numbers, and ids

Run two or three differently-phrased queries in parallel rather than one long one. If nothing
scores above ~0.3, rephrase toward the outcome rather than the code.

## Freshness

The user may have `sb` aliased to `--auto-index`, and a `SessionEnd` hook may index each
transcript as it closes. Do not assume either is set up: search prints a staleness warning on
stderr when the index is behind.

Stale results are still usable. Re-index only when the session you need is minutes old.

```sh
sb status
sb index --prune
```

Prefer `--auto-index` on the search itself over a separate `index` call — it refreshes only the
stale sources and then answers in one step.

## Resume

Provider is inferred from the index:

```sh
sb resume 8d162cb3-2241-45c2-8076-bde2a62b4588 --print-command
```

Claude resume is project-directory scoped. If the original worktree is gone, the output includes
a `git worktree add ...` restore line to run first.

Never resume on the user's behalf without asking — use `--print-command` and surface it.

## TUI (interactive — for the user, not for you)

```sh
sb tui
```

`Ctrl-R` resume in a tmux pane, `Ctrl-O` resume externally, `Ctrl-W` restore a missing worktree,
`Ctrl-P` preview, `Esc` quit. Do not launch this from an agent turn — it takes over the terminal.
Hand the user the command to run themselves when they want to browse rather than get one answer.

## Reporting back

Report **id, provider, date, branch/cwd**, and what the session concluded. Ids alone are useless.
When several sessions cover the same work, say how they relate — driver vs. child agent, first
pass vs. round two — rather than listing them flat.

## Gotchas

- **All providers.** Claude-only greps miss every Codex subagent, and every opencode and pi
  session. Do not filter to one provider unless asked.
- **Raw hit counts mislead.** The session mentioning a ticket most often is usually the
  coordinating session, not the one that did the work. Trust the score and the excerpts.
- **Subagent transcripts are indexed too**, under `<session>/subagents/agent-*.jsonl`. The real
  work often lives there while the parent holds only the handoff message.
- **The branch a session started on is not the branch it worked on.** `--branch` covers
  branches created, pushed or checked out mid-session; the `branches:` line under each hit
  shows how each was seen. Do not conclude a branch is unused from a query against one
  session's starting branch.
- **Interactive TUI use leaves no transcript.** A missing session proves nothing about whether
  the user did that work by hand.
