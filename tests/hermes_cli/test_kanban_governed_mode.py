"""Slip #002 — Governed Mode regression suite (Council/Command Kanban).

RED-first: each test pins the fail-closed governed-mode contract that closes
one of the four reproduced Phase-B1 failure classes, plus a back-compat
control proving non-governed (legacy) behavior is unchanged.

Governed mode is opt-in per task/board; a governed task starts
execution-unauthorized and must be explicitly released before it can run.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn(*args, **kwargs):
    """Stand-in worker spawn — returns a fake PID so a real spawn is a
    fake_spawn call we can count."""
    return 4242


# ---------------------------------------------------------------------------
# Class 1 — auto-promotion before explicit release
# ---------------------------------------------------------------------------


def test_governed_task_not_promoted_or_claimable_until_release(kanban_home):
    """A governed child whose parents are all done must NOT reach ``ready``
    (no dispatcher tick may claim/spawn it) until a durable explicit release.
    Parent completion alone must never grant execution authority."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        child = kb.create_task(
            conn, title="governed child", assignee="default",
            parents=(parent,), governed=True,
        )
        # Parent done -> recompute would normally promote the child to ready.
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        row = conn.execute("SELECT status, execution_authorized FROM tasks WHERE id=?", (child,)).fetchone()
        assert row["status"] == "todo", "governed unreleased child must not auto-promote to ready"
        assert row["execution_authorized"] == 0

    # A full dispatcher tick must not claim/spawn the unreleased governed task.
    with kbc.connect() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert all(child != s[0] for s in res.spawned), "governed unreleased task must not spawn"

    # Explicit release is the only thing that grants readiness.
    with kbc.connect() as conn:
        ok, reason = kb.release_task(conn, child, actor="hermes", reason="approved")
        assert ok, reason
        kb.recompute_ready(conn)
        row = conn.execute("SELECT status, execution_authorized FROM tasks WHERE id=?", (child,)).fetchone()
        assert row["status"] == "ready"
        assert row["execution_authorized"] == 1
        evs = [e["kind"] for e in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (child,))]
        assert "released" in evs


# ---------------------------------------------------------------------------
# Class 3 — unauthorized auto-decompose / LLM reassignment
# ---------------------------------------------------------------------------


def test_governed_triage_not_auto_decomposed_or_spawned(kanban_home):
    """A governed triage task is never picked up by the auto-decomposer and
    never auto-reassigned/promoted/spawned on a watcher/dispatcher tick."""
    with kbc.connect() as conn:
        gov = kb.create_task(conn, title="governed triage", triage=True, governed=True)
        legacy = kb.create_task(conn, title="legacy triage", triage=True)

    # The decomposer's triage lister must exclude governed tasks.
    from hermes_cli import kanban_decompose as decomp
    ids = decomp.list_triage_ids()
    assert gov not in ids, "governed triage must not be offered to the auto-decomposer"
    assert legacy in ids, "non-governed triage remains auto-decomposable"

    # A direct decompose of a governed root must refuse (defense at the mutation).
    outcome = decomp.decompose_task(gov, author="auto-decomposer")
    assert not outcome.ok
    with kbc.connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (gov,)).fetchone()
        assert row["status"] == "triage", "governed task must stay in triage, unmodified"
        decomposed = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='decomposed'", (gov,)
        ).fetchone()
        assert decomposed is None


# ---------------------------------------------------------------------------
# Class 2 — same-profile review auto-redispatch
# ---------------------------------------------------------------------------


def test_governed_review_requires_distinct_reviewer_and_holds(kanban_home):
    """A governed review cannot use the implementer as reviewer, and a governed
    review is not auto-dispatched to a worker without explicit assignment."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gov review", assignee="alice", governed=True)
        kb.release_task(conn, tid, actor="hermes", reason="go")
        kb.claim_task(conn, tid)  # ready -> running under alice

        # Same-profile reviewer must be refused.
        ok, reason = kb.request_review(conn, tid, reviewer="alice", force=True, with_reason=True)
        assert not ok, "governed review must reject implementer==reviewer"

        # A distinct reviewer is accepted.
        ok, reason = kb.request_review(conn, tid, reviewer="bob", force=True, with_reason=True)
        assert ok, reason
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "review"
        assert row["assignee"] == "bob"

    # A dispatcher tick must NOT auto-spawn a worker on the governed review task.
    with kbc.connect() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert all(tid != s[0] for s in res.spawned), "governed review must not auto-redispatch"


# ---------------------------------------------------------------------------
# Class 4 — done-before-acceptance / durable origin + named acceptance owner
# ---------------------------------------------------------------------------


def test_governed_conversation_task_cannot_complete_direct_to_done(kanban_home):
    """A conversation-originated governed task cannot go straight to Done: it
    enters delivery/acceptance pending, and only its named acceptance owner
    finalizes."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="gov convo task", assignee="alice", governed=True,
            session_id="sess-abc",  # conversation origin
        )
        kb.release_task(conn, tid, actor="hermes", reason="go")
        kb.claim_task(conn, tid)
        # A delivery-required task needs a durable deliverable before it can
        # enter a pending state (nothing to prove delivered otherwise).
        kb.add_attachment(conn, tid, filename="result.md", stored_path="/tmp/result.md", size=3)
        # Executor completion reaches a PENDING state only, never Done.
        kb.complete_task(conn, tid, result="did the work")
        row = conn.execute("SELECT status, acceptance_owner FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "delivery_pending", "governed convo task must not be Done on executor completion"
        assert row["acceptance_owner"] == "hermes", "internal governed work defaults to hermes acceptance owner"


# ---------------------------------------------------------------------------
# Back-compat — non-governed legacy behavior unchanged
# ---------------------------------------------------------------------------


def test_nongoverned_legacy_behavior_unchanged(kanban_home):
    """A non-governed task promotes, claims, and completes exactly as today —
    no governed guard changes the legacy path."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="p", assignee="default")
        child = kb.create_task(conn, title="c", assignee="default", parents=(parent,))
        # Not done yet -> child parked in todo.
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()["status"] == "todo"
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        # Legacy: parent-done alone promotes to ready (no release needed).
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()["status"] == "ready"
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (child,)).fetchone()["execution_authorized"] == 1
        # And it claims + completes straight to done (no delivery gate).
        kb.claim_task(conn, child)
        kb.complete_task(conn, child, result="done")
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()["status"] == "done"


# ---------------------------------------------------------------------------
# CLI surface — `kanban create --governed` + `kanban release` end-to-end
# ---------------------------------------------------------------------------


def test_cli_governed_create_and_release_round_trip(kanban_home):
    """The real CLI entry (run_slash, used by both CLI and gateway) creates a
    governed, fail-closed task and releases it via an explicit `release`."""
    from hermes_cli import kanban as kc

    out = kc.run_slash("create 'gov cli task' --assignee alice --governed --json")
    tid = json.loads(out)["id"]
    with kbc.connect() as conn:
        row = conn.execute("SELECT status, governed, execution_authorized, acceptance_owner FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["governed"] == 1
    assert row["execution_authorized"] == 0, "governed task must be created execution-unauthorized"
    assert row["status"] != "ready", "governed task must not be dispatch-ready before release"
    assert row["acceptance_owner"] == "hermes"

    # A delegated child must not be able to self-release (authority action).
    assert "release" in kc._DELEGATED_CHILD_DENIED_ACTIONS

    rel = kc.run_slash(f"release {tid} approved-by-hermes")
    assert "Released" in rel
    with kbc.connect() as conn:
        row = conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["execution_authorized"] == 1
        evs = [e["kind"] for e in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "released" in evs


# ---------------------------------------------------------------------------
# Class 4 (part 2) — named acceptance owner is ENFORCED at the accept boundary
# ---------------------------------------------------------------------------


def _deliver_governed_task(conn, *, acceptance_owner):
    """Drive a governed conversation-origin task to awaiting_acceptance with a
    delivered telegram outbox + subscription, ready for an accept attempt."""
    from hermes_cli import kanban_db_notify as kbn
    tid = kb.create_task(
        conn, title="gov deliver", assignee="alice", governed=True,
        session_id="sess-x", acceptance_owner=acceptance_owner,
    )
    kb.release_task(conn, tid, actor="hermes", reason="go")
    kb.claim_task(conn, tid)
    kb.add_attachment(conn, tid, filename="r.md", stored_path="/tmp/r.md", size=2)
    assert kb.complete_task(conn, tid, summary="done")
    assert kb.record_outbox_delivery(
        conn, tid, platform="telegram", conversation_ref="chat-9", thread_id=None,
        subscription_identity="user:wessam", session_ref="s-9", native_message_id="out-9",
    )
    kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-9", user_id="wessam")
    return tid


def test_governed_acceptance_rejected_for_non_owner_and_accepted_for_owner(kanban_home):
    """A governed task named to owner 'wessam' cannot be finalized by a different
    accepting owner; the named owner succeeds."""
    with kbc.connect() as conn:
        tid = _deliver_governed_task(conn, acceptance_owner="wessam")

    # Wrong owner -> fail closed, task stays awaiting_acceptance, reject event logged.
    with kbc.connect() as conn:
        assert not kb.accept_delivery_from_gateway(
            conn, tid, platform="telegram", chat_id="chat-9", thread_id=None,
            user_id="wessam", user_id_alt=None, inbound_message_id="in-a",
            accepting_owner="atlas",
        )
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "awaiting_acceptance"
        kinds = [e["kind"] for e in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "acceptance_rejected" in kinds

    # Named owner -> accepted to done.
    with kbc.connect() as conn:
        assert kb.accept_delivery_from_gateway(
            conn, tid, platform="telegram", chat_id="chat-9", thread_id=None,
            user_id="wessam", user_id_alt=None, inbound_message_id="in-b",
            accepting_owner="wessam",
        )
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "done"


def test_nongoverned_delivery_acceptance_ignores_accepting_owner(kanban_home):
    """Back-compat: a non-governed delivery-required task accepts without any
    acceptance-owner constraint (accepting_owner is irrelevant)."""
    from hermes_cli import kanban_db_notify as kbn
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legacy deliver", delivery_required=True)
        kb.add_attachment(conn, tid, filename="r.md", stored_path="/tmp/r.md", size=2)
        assert kb.complete_task(conn, tid, summary="done")
        assert kb.record_outbox_delivery(
            conn, tid, platform="telegram", conversation_ref="chat-3", thread_id=None,
            subscription_identity="user:wessam", session_ref="s-3", native_message_id="out-3",
        )
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-3", user_id="wessam")
    with kbc.connect() as conn:
        # No accepting_owner supplied at all — legacy path unchanged, accepts.
        assert kb.accept_delivery_from_gateway(
            conn, tid, platform="telegram", chat_id="chat-3", thread_id=None,
            user_id="wessam", user_id_alt=None, inbound_message_id="in-1",
        )
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "done"


# ---------------------------------------------------------------------------
# Class 1 (part 2) — unblock/reopen cannot queue a governed unreleased task
# ---------------------------------------------------------------------------


def test_unblock_holds_governed_unreleased_task_in_todo(kanban_home):
    """Unblocking a governed, unreleased task must land it in 'todo', not 'ready'
    — otherwise it would spin the dispatcher's claim-reject loop."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gov blocked", assignee="alice", governed=True)
        # A governed unreleased task cannot be claimed; put it directly in blocked
        # (a real human hold), then unblock.
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        assert kb.unblock_task(conn, tid)
        row = conn.execute("SELECT status, execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "todo", "governed unreleased task must not be queued to ready on unblock"
        assert row["execution_authorized"] == 0

