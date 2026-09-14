"""Slip #007 — server-owned board boundary: capability-authorized governed
release + durable internal acceptance route.

These tests exercise the REAL in-process path (real imports, temp HERMES_HOME).
They prove the parts that are genuinely enforceable in-process:
- release authority is possession of the service capability, NOT profile identity;
- caller-supplied identity/actor cannot authorize;
- internal (no-session) governed work has an end-to-end acceptance route and no
  longer strands at delivery_pending;
- the CLI-as-client fails closed when no trusted service is reachable.

The OS-ACL/separate-Windows-identity portion (that a same-user process cannot
READ the capability file or RAW-WRITE the DB) is OPS #007-OPS and is NOT claimed
here — see GOVERNED_SERVICE_BOUNDARY_DESIGN.md and the Slip #006 strict-xfails
that remain xfail on a single-user box.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_authority as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    ka._reset_for_tests()
    yield home
    ka._reset_for_tests()


# --- capability unit -------------------------------------------------------

def test_capability_mint_is_stable_and_verifies(kanban_home):
    cap = ka.mint_service_capability()
    assert cap and len(cap) >= 32
    # Adopt-on-restart: minting again returns the same persisted secret.
    assert ka.mint_service_capability() == cap
    assert ka.verify_capability(cap) is True
    assert ka.verify_capability("wrong") is False
    assert ka.verify_capability(None) is False
    assert ka.verify_capability("") is False


def test_capability_file_not_world_readable_posix(kanban_home):
    import sys
    if sys.platform == "win32":
        pytest.skip("POSIX mode bits only; Windows protection is the OPS #007 ACL")
    ka.mint_service_capability()
    mode = ka.capability_path().stat().st_mode & 0o777
    assert mode == 0o600, f"capability file must be owner-only, got {oct(mode)}"


def test_bare_process_holds_no_capability(kanban_home):
    """A process that never minted the capability cannot self-assert service
    context (an ordinary agent/CLI)."""
    ka._reset_for_tests()
    assert ka.in_process_capability() is None
    assert ka.is_service_context() is False


# --- release authority is the capability, not identity ---------------------

def test_release_requires_capability_not_identity(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gov", governed=True, assignee="relay")
        # Forged 'hermes' identity, no capability -> rejected (Slip #005/#006 spoof dead).
        ok, _ = kb.release_task(conn, tid, actor="hermes", caller_identity="hermes")
        assert ok is False
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 0
        # Correct capability -> released.
        ok2, err = kb.release_task(conn, tid, actor="hermes", capability=ka.mint_service_capability())
        assert ok2, err
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 1
        # Audit records provenance but never as authority.
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='released'", (tid,)).fetchone()
        assert ev is not None


# --- internal acceptance route (Slip #005 HIGH #2 liveness) ----------------

def test_internal_governed_end_to_end_acceptance(kanban_home):
    """create -> release (capability) -> claim -> complete -> delivery_pending
    -> named-owner internal acceptance -> done. No conversation subscription."""
    cap = ka.mint_service_capability()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="internal gov e2e", governed=True, assignee="relay")
        assert kb.release_task(conn, tid, actor="svc", capability=cap)[0]
        kb.claim_task(conn, tid)
        kb.add_attachment(conn, tid, filename="r.md", stored_path="/tmp/r.md", size=2)
        assert kb.complete_task(conn, tid, summary="internal done")
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"

        # Wrong owner -> fail closed, stays delivery_pending.
        assert kb.accept_internal_governed_delivery(conn, tid, accepting_owner="mallory", capability=cap) is False
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"

        # No capability -> fail closed.
        assert kb.accept_internal_governed_delivery(conn, tid, accepting_owner="hermes") is False
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"

        # Named owner + capability -> done, durable acceptance recorded.
        assert kb.accept_internal_governed_delivery(conn, tid, accepting_owner="hermes", capability=cap) is True
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "done"
        acc = kb.get_task_acceptance(conn, tid)
        assert acc is not None


def test_internal_acceptance_rejects_conversation_origin(kanban_home):
    """A task WITH a session_id must not be finalized by the internal route —
    that path is for no-session internal work only."""
    cap = ka.mint_service_capability()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="convo gov", governed=True, assignee="relay", session_id="sess-1")
        assert kb.release_task(conn, tid, actor="svc", capability=cap)[0]
        kb.claim_task(conn, tid)
        kb.add_attachment(conn, tid, filename="r.md", stored_path="/tmp/r.md", size=2)
        assert kb.complete_task(conn, tid, summary="done")
        # Internal route refuses a conversation-origin task.
        assert kb.accept_internal_governed_delivery(conn, tid, accepting_owner="hermes", capability=cap) is False
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"


# --- CLI-as-client fails closed with no service ----------------------------

def test_cli_release_fails_closed_when_no_service_reachable(kanban_home):
    """The CLI holds no capability and no gateway answers the control socket, so
    `release` fails closed (never falls back to identity)."""
    from hermes_cli import kanban as kc
    ka._reset_for_tests()
    import json
    out = kc.run_slash("create 'gov' --assignee relay --governed --json")
    tid = json.loads(out)["id"]
    rel = kc.run_slash(f"release {tid} attempt")
    assert "cannot release" in rel.lower(), rel
    with kbc.connect() as conn:
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 0
