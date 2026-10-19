from __future__ import annotations

import pytest

from session_buddy.branches import extract_branches, normalise
from session_buddy.database import IndexDatabase
from session_buddy.models import MessageRecord, SessionRecord
from session_buddy.search import search_sessions


def record(*texts: str, git_branch: str = "start-branch", session_id: str = "s1") -> SessionRecord:
    messages = [
        MessageRecord(provider="claude", session_id=session_id, idx=i, role="assistant",
                      timestamp=i, text=text)
        for i, text in enumerate(texts)
    ]
    return SessionRecord(
        provider="claude", session_id=session_id, title="t", cwd="/repo",
        created_at=1, updated_at=2, git_branch=git_branch, source_path=f"/tmp/{session_id}",
        file_mtime=1, file_size=1, message_count=len(messages), first_prompt="",
        summary="", preview="webhook retry", messages=messages,
    )


def branches(rec: SessionRecord) -> set[tuple[str, str]]:
    return {(b.branch, b.evidence) for b in extract_branches(rec)}


def test_the_start_branch_is_recorded():
    assert ("start-branch", "start") in branches(record())


def test_push_output_new_branch():
    text = " * [new branch]      dis-1269-processable-amount-threshold -> dis-1269-processable-amount-threshold"

    assert ("dis-1269-processable-amount-threshold", "push-output") in branches(record(text))


def test_push_output_forced_update():
    """The shape that motivated the feature: the branch only appears in push output."""
    text = " + e4184182d3...a26a7f2368 dis-1269-processable-amount-threshold -> dis-1269-processable-amount-threshold (forced update)"

    assert ("dis-1269-processable-amount-threshold", "push-output") in branches(record(text))


def test_push_command_against_a_named_remote():
    assert ("feature-x", "push") in branches(record("git push -u origin feature-x"))


def test_worktree_add_both_forms():
    with_b = branches(record("git worktree add -b feat/new ../wt"))
    positional = branches(record("git worktree add ../wt feat/existing"))

    assert ("feat/new", "worktree") in with_b
    assert ("feat/existing", "worktree") in positional


def test_status_and_checkout():
    assert ("release-1", "status") in branches(record("On branch release-1"))
    assert ("feat/x", "checkout") in branches(record("git checkout -b feat/x"))
    assert ("feat/y", "checkout") in branches(record("git switch -c feat/y"))


def test_main_is_a_real_branch_not_noise():
    assert ("main", "status") in branches(record("On branch main"))


@pytest.mark.parametrize(
    "text",
    [
        "git push --force-with-lease origin",          # no branch argument
        "git push 186 sessions",                        # prose that mentions the command
        "worktree add /tmp/mainwt",                     # a path, no branch
    ],
)
def test_loose_prose_and_flags_are_not_branches(text):
    """Earlier patterns turned `-u`, `origin`, `/tmp/wt` and stray words into branches."""
    found = {b.branch for b in extract_branches(record(text))}

    assert found <= {"start-branch"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("origin/feature", "feature"),
        ("refs/heads/feature", "feature"),
        ("feat/nested/name", "feat/nested/name"),   # a real namespace, not a remote
        ("dominikj/thing", "dominikj/thing"),
        ("'quoted'", "quoted"),
        ("-u", ""),
        ("--force", ""),
        ("/tmp/wt", ""),
        ("a26a7f2368601c0e5f273d127ae0af6635ae5e55", ""),
        ("1234", ""),
        ("origin", ""),
        ("HEAD", ""),
        ("x" * 300, ""),
        ("feature.lock", ""),
        ("a..b", ""),
    ],
)
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_branches_are_stored_and_refreshed_on_reindex(tmp_path):
    db = IndexDatabase(tmp_path / "index.sqlite")
    try:
        db.upsert_session(record("git checkout -b first"), embedder=None)
        assert {b.branch for b in db.branches_for("claude", "s1")} == {"start-branch", "first"}

        db.upsert_session(record("git checkout -b second"), embedder=None)

        # re-parsing the transcript replaces the old set rather than accumulating
        assert {b.branch for b in db.branches_for("claude", "s1")} == {"start-branch", "second"}
    finally:
        db.close()


def test_branch_filter_matches_a_branch_touched_mid_session(tmp_path):
    """A session that started elsewhere and pushed the branch is still found."""
    db = IndexDatabase(tmp_path / "index.sqlite")
    try:
        db.upsert_session(
            record(" * [new branch] dis-1269-amount -> dis-1269-amount",
                   git_branch="dis-1377-other", session_id="worked"),
            embedder=None,
        )
        db.upsert_session(record("unrelated", session_id="other"), embedder=None)
        db.conn.commit()
        path = db.path
    finally:
        db.close()

    hits = search_sessions(path, "webhook", branch="dis-1269", semantic=False)

    assert [h.session_id for h in hits] == ["worked"]


def test_branch_filter_accepts_a_glob(tmp_path):
    db = IndexDatabase(tmp_path / "index.sqlite")
    try:
        db.upsert_session(record("On branch feat/alpha", session_id="a"), embedder=None)
        db.upsert_session(record("On branch bugfix/beta", session_id="b"), embedder=None)
        db.conn.commit()
        path = db.path
    finally:
        db.close()

    assert [h.session_id for h in search_sessions(path, "webhook", branch="feat/*", semantic=False)] == ["a"]


# --- flattened transcript text -------------------------------------------------------------
# `collapse_ws` removes every newline before a message is stored, so a pattern anchored with
# `^` and re.M only matches at offset 0 of the whole message. Measured before this fix:
# 175 of 188 `[new branch]` mentions were unreachable and `On branch` had produced no rows.

FLAT_PUSH = (
    "To https://github.com/acme/repo.git * [new branch] acme-142-retry -> acme-142-retry "
    "branch 'acme-142-retry' set up to track 'origin/acme-142-retry'. "
    "To https://github.com/acme/repo.git * [new branch] acme-143-gate -> acme-143-gate "
    "branch 'acme-143-gate' set up to track 'origin/acme-143-gate'."
)


def test_push_output_is_found_without_a_line_start():
    """The real stored shape: one line, the marker preceded by the remote URL."""
    found = {(b.branch, b.evidence) for b in extract_branches(record(FLAT_PUSH))}

    assert ("acme-142-retry", "push-output") in found


def test_every_push_in_a_flattened_message_is_found():
    """Three branches pushed in one tool result must not collapse to the first."""
    branches = {b.branch for b in extract_branches(record(FLAT_PUSH))}

    assert {"acme-142-retry", "acme-143-gate"} <= branches


def test_set_up_to_track_is_its_own_evidence():
    """`push -u` output where the [new branch] line is absent from the captured text."""
    text = "branch 'acme-144-only-tracked' set up to track 'origin/acme-144-only-tracked'."

    found = {(b.branch, b.evidence) for b in extract_branches(record(text))}

    assert ("acme-144-only-tracked", "push-output") in found


def test_on_branch_is_found_mid_string():
    """`^On branch …$` matched nothing once newlines were gone."""
    text = "$ git status On branch acme-145-status Your branch is up to date."

    found = {(b.branch, b.evidence) for b in extract_branches(record(text))}

    assert ("acme-145-status", "status") in found


def test_a_placeholder_name_is_not_a_branch():
    """Transcripts discuss these patterns, so prose about them matches them."""
    text = "the regex is branch 'X' set up to track, which only a real push emits"

    assert {b.branch for b in extract_branches(record(text))} == {"start-branch"}
