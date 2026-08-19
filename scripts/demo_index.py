"""Build a small index of invented sessions, for README output and screenshots.

Everything here is fictional. Run it, point the CLI or TUI at the result, and you get
real output from real code without exposing anyone's actual session history:

    uv run python scripts/demo_index.py /tmp/demo.sqlite
    sb --db /tmp/demo.sqlite "webhook retry" --show-ext
    sb --db /tmp/demo.sqlite tui
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from session_buddy.database import IndexDatabase  # noqa: E402
from session_buddy.models import AnnotatedTitle, Annotation, MessageRecord, SessionRecord  # noqa: E402

DAY = 86_400_000
BASE = 1_771_000_000_000  # a fixed instant, so screenshots are reproducible

EPIC = "a1b2c3d4-0000-4000-8000-00000000ab01"
EPIC_TITLE = "Payments Integration Analysis"

SESSIONS = [
    dict(
        provider="claude", sid="7c1f9a30-1111-4a00-9c00-0000000000a1", days=0.2,
        title="ACME-142 retry backoff gate", cwd="/Users/dev/acme-mono", branch="acme-142-retry-backoff",
        turns=[
            ("user", "gate delivery on the retry budget so a dead endpoint stops burning attempts"),
            ("assistant", "Added isRetryBudgetAvailable to the delivery evaluation and threaded it through the scheduler."),
            ("user", "what happens when the budget lookup itself fails?"),
            ("assistant", "It fails closed now — a throwing lookup blocks delivery instead of silently allowing it."),
        ],
        epic=True, agent_title="ACME-142 retry backoff gate", kind="agent",
    ),
    dict(
        provider="codex", sid="019f0000-0000-7000-8000-000000000001", days=1.1,
        title="PR 128 Alignment Review", cwd="/Users/dev/acme-mono", branch="main",
        turns=[
            ("user", "review PR 128 (main...retry-backoff) for alignment with the delivery contract"),
            ("assistant", "One defect: the backoff timestamp is recorded before the attempt, so a crash mid-attempt loses the retry."),
        ],
        epic=True, agent_title="PR 128 Alignment Review", kind="chat",
    ),
    dict(
        provider="claude", sid="7c1f9a30-2222-4a00-9c00-0000000000b2", days=2.4,
        title="why webhooks were not retried after a failed delivery", cwd="/Users/dev/acme-mono",
        branch="main",
        turns=[
            ("user", "why webhooks were not retried after a failed delivery"),
            ("assistant", "The queue drops events whose subscription was disabled between enqueue and drain; the drain filter reads live state."),
        ],
    ),
    dict(
        provider="opencode", sid="ses_00a1b2c3d4e5f60718293a4b5c", days=3.0,
        title="Refactor the billing adapter", cwd="/Users/dev/acme-api", branch="refactor-billing-adapter",
        turns=[
            ("user", "split the billing adapter so the provider client is injectable"),
            ("assistant", "Extracted BillingClient behind a port; the adapter now takes it in the constructor."),
        ],
    ),
    dict(
        provider="codex", sid="019f0000-0000-7000-8000-000000000002", days=4.5,
        title="ACME-206 idempotency keys", cwd="/Users/dev/acme-api", branch="acme-206-idempotency",
        turns=[
            ("user", "add idempotency keys to the charge endpoint"),
            ("assistant", "Keys are stored with the request hash; a replay with a different body now 409s rather than double-charging."),
        ],
    ),
    dict(
        provider="claude", sid="7c1f9a30-3333-4a00-9c00-0000000000c3", days=6.2,
        title="flaky test triage: order sync", cwd="/Users/dev/acme-mono", branch="main",
        turns=[
            ("user", "order sync suite fails about one run in five in CI, green locally"),
            ("assistant", "Shared fixture clock: two tests advance it concurrently. Pinned the clock per test."),
        ],
    ),
    dict(
        provider="claude", sid="7c1f9a30-4444-4a00-9c00-0000000000d4", days=8.0,
        title="ACME-155 rate limiter rollout", cwd="/Users/dev/acme-mono", branch="acme-155-rate-limiter",
        turns=[
            ("user", "roll the rate limiter out behind a flag, 5% first"),
            ("assistant", "Flag added with a per-tenant bucket; the 5% cohort is keyed on tenant id so a tenant never flips mid-window."),
        ],
        epic=True, agent_title="ACME-155 rate limiter rollout", kind="agent",
    ),
    dict(
        provider="codex", sid="019f0000-0000-7000-8000-000000000003", days=11.0,
        title="docs: API pagination", cwd="/Users/dev/acme-api", branch="docs-pagination",
        turns=[
            ("user", "document cursor pagination on the public API"),
            ("assistant", "Added a pagination section with a worked example and the stability caveat for cursors."),
        ],
    ),
]


def build(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    db = IndexDatabase(db_path)
    annotations: list[Annotation] = []
    titles: list[AnnotatedTitle] = []

    for spec in SESSIONS:
        updated = int(BASE - spec["days"] * DAY)
        messages = [
            MessageRecord(
                provider=spec["provider"], session_id=spec["sid"], idx=i,
                role=role, timestamp=updated - (len(spec["turns"]) - i) * 60_000, text=text,
            )
            for i, (role, text) in enumerate(spec["turns"])
        ]
        first_prompt = next(m.text for m in messages if m.role == "user")
        db.upsert_session(
            SessionRecord(
                provider=spec["provider"], session_id=spec["sid"], title=spec["title"],
                cwd=spec["cwd"], created_at=updated - 3_600_000, updated_at=updated,
                git_branch=spec["branch"], source_path=f"/demo/{spec['sid']}",
                file_mtime=updated, file_size=len(first_prompt), message_count=len(messages),
                first_prompt=first_prompt, summary="",
                preview=" ".join(m.text for m in messages)[:300], messages=messages,
            ),
            embedder=None,
        )
        if spec.get("epic"):
            kind = spec["kind"]
            for key, value in {
                "epic_id": EPIC, "epic_title": EPIC_TITLE, "kind": kind,
                f"{kind}_title": spec["agent_title"], "workspace": spec["cwd"],
            }.items():
                annotations.append(Annotation(spec["provider"], spec["sid"], "traycer", key, value))
            titles.append(
                AnnotatedTitle(spec["provider"], spec["sid"], "traycer", spec["agent_title"], 10)
            )

    db.replace_annotations("traycer", annotations)
    db.apply_annotated_titles(titles)
    db.conn.commit()
    db.close()
    print(f"demo index written: {db_path} ({len(SESSIONS)} sessions, {len(annotations)} annotations)")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/session-buddy-demo.sqlite"))
