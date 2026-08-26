"""Branch extraction.

`sessions.git_branch` is the branch a session *started* on — the providers record it once,
at launch. A session that creates a branch, adds a worktree, or pushes somewhere else leaves
no trace in that column, so filtering on it answers the wrong question:

    a session that started on `dis-1377-hbc-read-semantics` authored and pushed
    `dis-1269-processable-amount-threshold`, and a query on git_branch found nothing.

So branches are also read out of the transcript, one row per (session, branch, evidence).

Which patterns matter is a measurement, not a guess. Across a real index:

    git push        186 sessions      worktree add     75 sessions
    On branch        98 sessions      switch -c        32 sessions
    checkout -b      17 sessions      gh pr checkout    2 sessions

`checkout -b` is the obvious one and it is nearly worthless here — it had *zero* hits in the
session that motivated this, because branches usually arrive with a new worktree rather than
a checkout in place. Push output and `worktree add` carry the signal.

Evidence is stored alongside each branch. A name seen in push output is a strong claim; one
seen in prose is not, which is why prose is not scanned at all.
"""

from __future__ import annotations

import re

from .models import BranchRef, SessionRecord

# A git branch may not contain a space, `~^:?*[`, or `..`, and may not end in `/` or `.lock`.
BRANCH = r"[A-Za-z0-9_./+-]+"
SHA = r"[0-9a-f]{7,40}"

# Precision over recall. Loose patterns pick up flags (`-u`), remotes (`origin`), paths
# (`/tmp/wt`) and ordinary prose — a transcript that *discusses* branches would otherwise
# manufacture dozens of them. Each pattern below anchors on a shape git itself emits.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # push output, the strongest signal:
    #   ` * [new branch]      feature -> feature`
    #   ` + e418418...a26a7f2 feature -> feature (forced update)`
    (
        "push",
        re.compile(
            rf"^\s*[*+!=-]?\s*(?:\[new branch\]|\[deleted\]|{SHA}\.\.\.?{SHA})\s+"
            rf"({BRANCH})\s+->\s+({BRANCH})",
            re.M,
        ),
    ),
    # explicit invocation against a named remote: `git push origin feature`
    ("push", re.compile(rf"git\s+push\s+(?:-{{1,2}}[A-Za-z-]+\s+)*(?:origin|upstream)\s+({BRANCH})")),
    # `git worktree add -b feature ../wt` — the -b form names the branch unambiguously
    ("worktree", re.compile(rf"worktree\s+add\s+(?:-{{1,2}}[A-Za-z-]+\s+)*-b\s+({BRANCH})")),
    # `git worktree add ../wt feature` — branch is the trailing token after a path
    ("worktree", re.compile(rf"worktree\s+add\s+(?:-{{1,2}}[A-Za-z-]+\s+)*\S*/\S*\s+({BRANCH})")),
    ("status", re.compile(rf"^On branch\s+({BRANCH})\s*$", re.M)),
    ("checkout", re.compile(rf"(?:checkout\s+-b|switch\s+-c)\s+({BRANCH})")),
)

# Real refs that say nothing about which work a session touched.
# `main` is a real branch someone can work on, so it stays. Remotes and pseudo-refs go.
IGNORED = {"head", "fetch_head", "orig_head", "origin", "upstream", "detached"}
MAX_BRANCH_LENGTH = 200
SHA_ONLY = re.compile(r"^[0-9a-f]{7,40}$")


def extract_branches(record: SessionRecord) -> list[BranchRef]:
    """Every branch this session can be shown to have touched, with how it was seen."""
    seen: dict[tuple[str, str], BranchRef] = {}

    def add(branch: str, evidence: str) -> None:
        name = normalise(branch)
        if not name:
            return
        key = (name, evidence)
        seen.setdefault(
            key, BranchRef(record.provider, record.session_id, name, evidence)
        )

    add(record.git_branch, "start")
    for message in record.messages:
        for evidence, pattern in PATTERNS:
            for match in pattern.finditer(message.text):
                for group in match.groups():
                    if group:
                        add(group, evidence)
    return list(seen.values())


def normalise(branch: str | None) -> str:
    """Strip ref decoration so `origin/x`, `refs/heads/x` and `x` are one branch.

    Returns "" for anything that is not plausibly a branch someone worked on: flags, paths,
    bare shas, numbers, and the ref names every repo has.
    """
    name = (branch or "").strip().strip("'\"`,.")
    if not name or name.startswith("-") or name.startswith("/") or name.startswith("."):
        return ""
    if SHA_ONLY.match(name) or name.isdigit():
        return ""
    for prefix in ("refs/heads/", "refs/remotes/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    if "/" in name:
        head, _, tail = name.partition("/")
        # Only strip a remote, never a real namespace like `feat/x` or `dominikj/x`.
        if head in {"origin", "upstream"} and tail:
            name = tail
    if len(name) > MAX_BRANCH_LENGTH or name.lower() in IGNORED:
        return ""
    if name.endswith("/") or name.endswith(".lock") or ".." in name:
        return ""
    if not any(ch.isalnum() for ch in name):
        return ""
    if name.lower() in IGNORED or SHA_ONLY.match(name):
        return ""
    return name
