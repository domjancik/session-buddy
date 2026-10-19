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


def session(db, session_id: str, cwd: str, evidence: list[str], branch: str = "acme-142"):
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
        "insert or replace into session_branches (provider, session_id, branch, evidence)"
        " values ('claude', ?, ?, ?)",
        [(session_id, branch, e) for e in evidence],
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


def test_the_session_in_the_branch_worktree_ranks_first(db):
    """The one signal a quoting session cannot reproduce: where it was running."""
    session(db, "quoter", "/repo/elsewhere", ["pr", "push", "push-output"])
    session(db, "worker", "/repo/.worktrees/acme-142-abc", ["push-output"])

    ranked = db.branch_owners("acme-142")

    assert ranked[0]["session_id"] == "worker"
    assert ranked[0]["in_worktree"] is True
    assert ranked[1]["session_id"] == "quoter"


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
    assert "2 candidates and none ran in the branch's worktree" in err
    assert "one" in out and "two" in out


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
