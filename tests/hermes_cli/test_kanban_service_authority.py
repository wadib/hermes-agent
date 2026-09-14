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


# --- Slip #009: confused-deputy verbs removed from the control socket -------

def test_control_socket_rejects_removed_privileged_verbs(kanban_home):
    """Slip #008 confused-deputy fix: the generic gateway control socket must NOT
    expose governed-release / governed-accept-internal. A raw JSON request for
    either is rejected as an unknown verb with ZERO mutation (no release, no
    acceptance row) — even when the board-owner capability exists in this
    process."""
    import json
    from gateway.control_socket import GatewayControlServer
    # Even with a minted capability present, the socket must not spend it.
    ka.mint_service_capability()
    server = GatewayControlServer(verb_handlers={})  # same shape as gateway now uses
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="deputy victim", governed=True, assignee="relay")

    for verb, extra in (("governed-release", {}),
                        ("governed-accept-internal", {"accepting_owner": "hermes"})):
        raw = json.dumps({"verb": verb, "id": 1, "task_id": tid, **extra}).encode("utf-8")
        resp = json.loads(server.handle_request_line(raw).decode("utf-8"))
        assert resp["ok"] is False, resp
        assert "unknown verb" in resp["error"].lower(), resp

    # Zero mutation: still unreleased, no acceptance row, not done.
    with kbc.connect() as conn:
        row = conn.execute("SELECT status, execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()
        assert dict(row) == {"status": "todo", "execution_authorized": 0}
        assert kb.get_task_acceptance(conn, tid) is None
    ka._reset_for_tests()


def test_submit_release_request_is_non_authoritative(kanban_home):
    """A durable release request records provenance only and never authorizes."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="req only", governed=True, assignee="relay")
        ok, _ = kb.submit_release_request(conn, tid, requested_by="relay", reason="please")
        assert ok
        row = conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["execution_authorized"] == 0
        kinds = [e["kind"] for e in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "release_requested" in kinds and "released" not in kinds


def test_should_mint_board_authority_only_for_default_home(kanban_home, tmp_path, monkeypatch):
    """Single-owner: only the default-home process is the board-authority owner;
    a secondary-profile home is NOT (so it never mints/holds authority)."""
    from hermes_cli import profiles
    # kanban_home fixture set HERMES_HOME to the default home -> owner.
    assert ka.should_mint_board_authority() is True
    # A profile home under profiles/<name> is not the default -> not owner.
    prof_home = tmp_path / ".hermes" / "profiles" / "relay"
    prof_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(prof_home))
    import importlib
    importlib.reload(importlib.import_module("hermes_constants"))
    assert ka.should_mint_board_authority() is False

def test_cli_non_owner_release_is_request_only_never_executes(kanban_home, monkeypatch):
    """Slip #009: a non board-owner CLI cannot execute release. It submits a
    durable NON-AUTHORITATIVE request (release_requested event) and the task
    stays unreleased — never auto-approves."""
    from hermes_cli import kanban as kc
    import json
    ka._reset_for_tests()
    # Force non-owner role regardless of the isolated-home path coincidence.
    monkeypatch.setattr(ka, "should_mint_board_authority", lambda home=None: False)
    monkeypatch.setattr("hermes_cli.kanban_authority.in_process_capability", lambda: None)
    out = kc.run_slash("create 'gov' --assignee relay --governed --json")
    tid = json.loads(out)["id"]
    rel = kc.run_slash(f"release {tid} attempt")
    assert "requested" in rel.lower(), rel
    with kbc.connect() as conn:
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 0
        kinds = [e["kind"] for e in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
    assert "release_requested" in kinds
    assert "released" not in kinds


def test_cli_board_owner_release_executes(kanban_home, monkeypatch):
    """When THIS process is the board-authority owner (holds the in-process
    capability), the CLI release executes directly."""
    from hermes_cli import kanban as kc
    import json
    cap = ka.mint_service_capability()  # become the owner (in-process capability)
    monkeypatch.setattr("hermes_cli.kanban_authority.in_process_capability", lambda: cap)
    out = kc.run_slash("create 'gov owner' --assignee relay --governed --json")
    tid = json.loads(out)["id"]
    rel = kc.run_slash(f"release {tid} go")
    assert "released" in rel.lower(), rel
    with kbc.connect() as conn:
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 1
    ka._reset_for_tests()
