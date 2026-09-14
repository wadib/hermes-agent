"""Slip #010 — LOCAL OPERATIONAL governed authority.

Scope (single-user trusted local computer): the control prevents ACCIDENTAL /
unapproved APPLICATION behavior — a non board-owner profile/app path releasing
or accepting governed work. It is NOT a defense against a malicious same-user
process (raw SQLite write, HERMES_HOME retarget, process debugging) — that is out
of scope and documented as a trusted-local limitation, not a defect.

These tests exercise the REAL in-process path (real imports, temp HERMES_HOME):
- governed release/accept is authorized ONLY by the trusted board-owner role
  (is_board_authority_owner); caller identity/actor never authorize;
- a non-owner path can only submit a durable, non-authoritative request;
- internal (no-session) governed work has an end-to-end acceptance route and no
  longer strands at delivery_pending;
- the confused-deputy control-socket verbs stay removed (Slip #008 fix holds).
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
    yield home


# --- board-owner role (operational, not security) --------------------------

def test_board_authority_owner_only_for_default_home(kanban_home, tmp_path, monkeypatch):
    """Only the default-home process is the board-owner; a secondary-profile home
    is not (so an ordinary profile agent/app path cannot release/accept)."""
    # kanban_home set HERMES_HOME to the default home -> owner.
    assert ka.is_board_authority_owner() is True
    prof_home = tmp_path / ".hermes" / "profiles" / "relay"
    prof_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(prof_home))
    import importlib
    importlib.reload(importlib.import_module("hermes_constants"))
    assert ka.is_board_authority_owner() is False


# --- release authority is the board-owner decision, not identity -----------

def test_release_requires_board_owner_authorization_not_identity(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gov", governed=True, assignee="relay")
        # A forged 'hermes' identity without board-owner authorization -> rejected.
        ok, _ = kb.release_task(conn, tid, actor="hermes", caller_identity="hermes")
        assert ok is False
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 0
        # authorized=False explicitly -> rejected.
        ok_f, _ = kb.release_task(conn, tid, actor="hermes", authorized=False)
        assert ok_f is False
        # Board-owner operational decision (authorized=True) -> released.
        ok2, err = kb.release_task(conn, tid, actor="hermes", authorized=True)
        assert ok2, err
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 1
        # Idempotent second release: no duplicate event.
        ok3, _ = kb.release_task(conn, tid, actor="hermes", authorized=True)
        assert ok3
        n = conn.execute("SELECT COUNT(*) AS n FROM task_events WHERE task_id=? AND kind='released'", (tid,)).fetchone()["n"]
        assert n == 1


# --- internal acceptance route (Slip #005 HIGH #2 liveness) ----------------

def test_internal_governed_end_to_end_acceptance(kanban_home):
    """create -> release (board-owner) -> claim -> complete -> delivery_pending
    -> named-owner internal acceptance -> done. No conversation subscription."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="internal gov e2e", governed=True, assignee="relay")
        assert kb.release_task(conn, tid, actor="svc", authorized=True)[0]
        kb.claim_task(conn, tid)
        kb.add_attachment(conn, tid, filename="r.md", stored_path="/tmp/r.md", size=2)
        assert kb.complete_task(conn, tid, summary="internal done")
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"

        # Wrong owner -> fail closed, stays delivery_pending.
        assert kb.accept_internal_governed_delivery(conn, tid, accepting_owner="mallory", authorized=True) is False
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"

        # Not board-owner -> fail closed.
        assert kb.accept_internal_governed_delivery(conn, tid, accepting_owner="hermes", authorized=False) is False
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"

        # Named owner + board-owner authorization -> done, durable acceptance recorded.
        assert kb.accept_internal_governed_delivery(conn, tid, accepting_owner="hermes", authorized=True) is True
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "done"
        assert kb.get_task_acceptance(conn, tid) is not None


def test_internal_acceptance_rejects_conversation_origin(kanban_home):
    """A task WITH a session_id must not be finalized by the internal route —
    that path is for no-session internal work only."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="convo gov", governed=True, assignee="relay", session_id="sess-1")
        assert kb.release_task(conn, tid, actor="svc", authorized=True)[0]
        kb.claim_task(conn, tid)
        kb.add_attachment(conn, tid, filename="r.md", stored_path="/tmp/r.md", size=2)
        assert kb.complete_task(conn, tid, summary="done")
        assert kb.accept_internal_governed_delivery(conn, tid, accepting_owner="hermes", authorized=True) is False
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"


# --- confused-deputy verbs stay removed from the control socket ------------

def test_control_socket_rejects_removed_privileged_verbs(kanban_home):
    """Slip #008 confused-deputy fix holds: the generic gateway control socket
    must NOT expose governed-release / governed-accept-internal. A raw JSON
    request for either is rejected as an unknown verb with ZERO mutation."""
    import json
    from gateway.control_socket import GatewayControlServer
    server = GatewayControlServer(verb_handlers={})  # same shape the gateway now uses
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="deputy victim", governed=True, assignee="relay")

    for verb, extra in (("governed-release", {}),
                        ("governed-accept-internal", {"accepting_owner": "hermes"})):
        raw = json.dumps({"verb": verb, "id": 1, "task_id": tid, **extra}).encode("utf-8")
        resp = json.loads(server.handle_request_line(raw).decode("utf-8"))
        assert resp["ok"] is False, resp
        assert "unknown verb" in resp["error"].lower(), resp

    with kbc.connect() as conn:
        row = conn.execute("SELECT status, execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()
        assert dict(row) == {"status": "todo", "execution_authorized": 0}
        assert kb.get_task_acceptance(conn, tid) is None


def test_submit_release_request_is_non_authoritative(kanban_home):
    """A durable release request records provenance only and never authorizes."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="req only", governed=True, assignee="relay")
        ok, _ = kb.submit_release_request(conn, tid, requested_by="relay", reason="please")
        assert ok
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 0
        kinds = [e["kind"] for e in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "release_requested" in kinds and "released" not in kinds


# --- CLI: board-owner releases; non-owner is request-only ------------------

def test_cli_non_owner_release_is_request_only_never_executes(kanban_home, monkeypatch):
    """A non board-owner CLI cannot execute release; it submits a durable
    NON-AUTHORITATIVE request and the task stays unreleased."""
    from hermes_cli import kanban as kc
    import json
    monkeypatch.setattr(ka, "is_board_authority_owner", lambda home=None: False)
    out = kc.run_slash("create 'gov' --assignee relay --governed --json")
    tid = json.loads(out)["id"]
    rel = kc.run_slash(f"release {tid} attempt")
    assert "requested" in rel.lower(), rel
    with kbc.connect() as conn:
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 0
        kinds = [e["kind"] for e in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
    assert "release_requested" in kinds and "released" not in kinds


def test_cli_board_owner_release_executes(kanban_home, monkeypatch):
    """When THIS process is the board-owner, the CLI release executes directly."""
    from hermes_cli import kanban as kc
    import json
    monkeypatch.setattr(ka, "is_board_authority_owner", lambda home=None: True)
    out = kc.run_slash("create 'gov owner' --assignee relay --governed --json")
    tid = json.loads(out)["id"]
    rel = kc.run_slash(f"release {tid} go")
    assert "released" in rel.lower(), rel
    with kbc.connect() as conn:
        assert conn.execute("SELECT execution_authorized FROM tasks WHERE id=?", (tid,)).fetchone()["execution_authorized"] == 1


def test_cli_accept_internal_board_owner_finalizes_and_non_owner_denied(kanban_home, monkeypatch):
    """The `accept-internal` CLI surface finalizes an internal (no-session)
    governed task from delivery_pending to done — board-owner only. A non-owner
    is denied and the task stays delivery_pending (invariant 5: internal work has
    a usable durable acceptance and does not deadlock)."""
    from hermes_cli import kanban as kc
    import json
    monkeypatch.setattr(ka, "is_board_authority_owner", lambda home=None: True)
    out = kc.run_slash("create 'internal gov cli' --assignee relay --governed --json")
    tid = json.loads(out)["id"]
    # Drive to delivery_pending as the board-owner.
    assert "released" in kc.run_slash(f"release {tid} go").lower()
    with kbc.connect() as conn:
        kb.claim_task(conn, tid)
        kb.add_attachment(conn, tid, filename="r.md", stored_path="/tmp/r.md", size=2)
        assert kb.complete_task(conn, tid, summary="done")
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"

    # Non-owner cannot accept-internal.
    monkeypatch.setattr(ka, "is_board_authority_owner", lambda home=None: False)
    denied = kc.run_slash(f"accept-internal {tid}")
    assert "cannot accept" in denied.lower(), denied
    with kbc.connect() as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "delivery_pending"

    # Board-owner finalizes to done via the CLI surface.
    monkeypatch.setattr(ka, "is_board_authority_owner", lambda home=None: True)
    accepted = kc.run_slash(f"accept-internal {tid}")
    assert "accepted" in accepted.lower(), accepted
    with kbc.connect() as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "done"
        assert kb.get_task_acceptance(conn, tid) is not None
    # A delegated child may not invoke this authority action.
    assert "accept-internal" in kc._DELEGATED_CHILD_DENIED_ACTIONS
