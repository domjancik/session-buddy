"""Who owns a branch.

A branch hit is not self-validating. A session that reads about work collects the branch names
it quoted: a lookup agent that queried this database acquired 9 rows of which 8 were false, and
this project's own development session carries 47, most of them test fixtures. So the question
is how firmly a session can be said to own a branch, not whether the name appears in it.
"""

from __future__ import annotations

import argparse

import pytest

from session_buddy.database import IndexDatabase
from session_buddy.models import BranchRef, MessageRecord, SessionRecord


def session(db, session_id: str, cwd: str, evidence: list[str], branch: str = "acme-142",
            seen_at: int | None = None):
    db.upsert_session(
        SessionRecord(
            provider="claude", session_id=session_id, title=f"s-{session_id}", cwd=cwd,
            created_at=1, updated_at=2, git_branch="", source_path=f"/tmp/{session_id}",
            file_mtime=1, file_size=1, message_count=1, first_prompt="", summary="",
            preview="", messages=[MessageRecord(provider="claude", session_id=session_id,
                                                idx=0, role="a", timestamp=1, text="x")],
        ),
        embedder=None,
    )
    db.conn.executemany(
        "insert or replace into session_branches"
        " (provider, session_id, branch, evidence, seen_at) values ('claude', ?, ?, ?, ?)",
        [(session_id, branch, e, seen_at) for e in evidence],
    )
    db.conn.commit()


@pytest.fixture()
def db(tmp_path):
    database = IndexDatabase(tmp_path / "index.sqlite")
    yield database
    database.close()


def test_quoted_only_is_marked(db):
    session(db, "quoter", "/repo/elsewhere", ["push-output"])

    row = db.branch_owners("acme-142")[0]

    assert row["quoted_only"] is True
    assert row["best"] == "push-output"


def test_a_command_is_not_quoted_only(db):
    session(db, "runner", "/repo/elsewhere", ["push", "push-output"])

    assert db.branch_owners("acme-142")[0]["quoted_only"] is False


def test_a_launch_directory_does_not_outrank_corroborated_evidence(db):
    """`cwd` is where a session was LAUNCHED, and agents cd constantly.

    Measured: 82% of command-evidence rows sit on a session launched elsewhere, and a worktree
    outlives the PR that created it - a later occupant was ranked above the real author 38 days
    on. The directory is shown, never ranked.
    """
    session(db, "author", "/repo/elsewhere", ["pr", "push", "worktree"])
    session(db, "later-occupant", "/repo/.worktrees/acme-142-abc", ["push-output"])

    ranked = db.branch_owners("acme-142")

    assert ranked[0]["session_id"] == "author"
    assert ranked[0]["corroboration"] == 3
    assert ranked[1]["launched_here"] is True, "still reported, just not ranked on"


def test_corroboration_counts_only_kinds_a_quote_cannot_stack(db):
    session(db, "quoter", "/repo/a", ["pr", "push-output"])
    session(db, "author", "/repo/b", ["pr", "push", "worktree"])

    ranked = db.branch_owners("acme-142")

    assert [r["session_id"] for r in ranked] == ["author", "quoter"]
    assert ranked[0]["corroboration"] == 3
    assert ranked[1]["corroboration"] == 1


def test_the_command_time_is_reported_for_matching_against_a_pr(db):
    """The check that has settled ownership every time: command time vs the PR's own."""
    session(db, "author", "/repo/a", ["pr"], seen_at=1_700_000_123_000)

    assert db.branch_owners("acme-142")[0]["command_at"] == 1_700_000_123_000


def test_stronger_evidence_ranks_first_when_no_worktree_matches(db):
    session(db, "quoter", "/repo/a", ["push-output"])
    session(db, "runner", "/repo/b", ["start"])

    assert [r["session_id"] for r in db.branch_owners("acme-142")] == ["runner", "quoter"]


def test_an_unknown_branch_has_no_owners(db):
    session(db, "s", "/repo/a", ["push"])

    assert db.branch_owners("never-pushed") == []


def test_cmd_owner_says_when_it_cannot_decide(db, capsys):
    """Commands are quotable, so a tie has to be reported rather than broken arbitrarily."""
    from session_buddy.cli import cmd_owner

    session(db, "one", "/repo/a", ["pr", "push-output"])
    session(db, "two", "/repo/b", ["pr", "push-output"])

    code = cmd_owner(argparse.Namespace(db=db.path, branch="acme-142"))

    out, err = capsys.readouterr()
    assert code == 0
    assert "share the strongest evidence" in err
    assert "match any `command at` time" in err
    assert "one" in out and "two" in out


def test_cmd_owner_does_not_call_launch_evidence_quotable(db, capsys):
    """`start` is launch metadata, not transcript text: nobody quoted their way into it."""
    from session_buddy.cli import cmd_owner

    session(db, "one", "/repo/a", ["start"])
    session(db, "two", "/repo/b", ["start"])

    cmd_owner(argparse.Namespace(db=db.path, branch="acme-142"))

    err = capsys.readouterr().err
    assert "quoted" not in err
    assert "tie on launch evidence alone" in err
    assert "none of them ran a command" in err


def test_cmd_owner_counts_only_the_tied_candidates(db, capsys):
    """The warning names how many share the top, not how many are above the quoters."""
    from session_buddy.cli import cmd_owner

    session(db, "one", "/repo/a", ["pr", "push"], seen_at=1_700_000_000_000)
    session(db, "two", "/repo/b", ["pr", "push"], seen_at=1_700_000_100_000)
    session(db, "weaker", "/repo/c", ["checkout"])

    cmd_owner(argparse.Namespace(db=db.path, branch="acme-142"))

    assert "2 candidates share the strongest evidence" in capsys.readouterr().err


def test_cmd_owner_points_at_transcript_less_authors_when_all_are_quotes(db, capsys):
    from session_buddy.cli import cmd_owner

    session(db, "quoter", "/repo/a", ["push-output"])

    cmd_owner(argparse.Namespace(db=db.path, branch="acme-142"))

    assert "sb agents --missing" in capsys.readouterr().err


def test_cmd_owner_reports_an_unknown_branch(db, capsys):
    from session_buddy.cli import cmd_owner

    code = cmd_owner(argparse.Namespace(db=db.path, branch="nope"))

    assert code == 1
    assert "No session touched nope." in capsys.readouterr().err


def test_a_tie_leads_with_the_earliest_command(db):
    """A quote of a `gh pr create` can only be written after the real one."""
    session(db, "quoter", "/repo/a", ["pr", "push-output"], seen_at=2_000_000_000_000)
    session(db, "author", "/repo/b", ["pr", "push-output"], seen_at=1_700_000_000_000)

    ranked = db.branch_owners("acme-142")

    assert [r["session_id"] for r in ranked] == ["author", "quoter"]
    assert ranked[0]["corroboration"] == ranked[1]["corroboration"], "still a tie, just ordered"


def test_a_candidate_with_no_command_time_sorts_after_one_with(db):
    session(db, "timeless", "/repo/a", ["pr", "push-output"])
    session(db, "timed", "/repo/b", ["pr", "push-output"], seen_at=1_700_000_000_000)

    assert [r["session_id"] for r in db.branch_owners("acme-142")] == ["timed", "timeless"]
