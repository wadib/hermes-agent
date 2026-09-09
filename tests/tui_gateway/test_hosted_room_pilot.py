"""Tests for the supported non-looping hosted-room pilot control (tui_gateway.hosted_room_pilot).

The full-lifecycle test drives the real registered ``groups.*`` handlers against an isolated,
disposable service (tmp HERMES_HOME + a deterministic in-process transport) — the same seam the
authenticated ``/api/ws`` transport uses, with no credential forging and no direct SQLite writes.
The remaining tests pin the non-looping guarantee, the driver-readiness gate, and error/edge
handling with lightweight stub servers.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import tui_gateway.server as srv
from tui_gateway import methods_groups
from tui_gateway.hosted_room_pilot import (
    RoomMember,
    RoomPilot,
    RoomPilotError,
    RoomPilotTimeout,
    TERMINAL_TURN_KINDS,
    run_room_pilot,
)


# --------------------------------------------------------------------------- isolated fixture


class _DeterministicRPC:
    """Same-gateway session transport that settles every member turn deterministically.

    Mirrors the in-process session adapter contract (tui_gateway.hosted_room_server_rpc) so a
    disposable pilot run reaches a real terminal member-turn without a live model or Council.
    """

    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str], dict] = {}
        self.prompts: list[tuple[str, str]] = []

    def resolve_exact(self, *, profile, title, source):
        return self.sessions.get((profile, title))

    def create(self, *, profile, title, source):
        session = {"session_id": f"{profile}-session", "title": title}
        self.sessions[(profile, title)] = session
        return session

    def resume(self, *, profile, session_id, source):
        return {"session_id": session_id}

    def submit(self, *, profile, session_id, prompt, source, task, execution_generation, on_terminal):
        self.prompts.append((profile, prompt))
        on_terminal({"status": "settled", "text": f"reply from {profile}"})
        return {"accepted": True}

    def history(self, *, profile, session_id, source):
        return []

    def info(self, *, profile, session_id, source):
        return {"active": False, "task_id": None}

    def interrupt(self, *, profile, session_id, source, expected_task_id):
        return {"interrupted": True}

    def approve(self, **kwargs):
        return {"resolved": 1}


@pytest.fixture
def isolated_service(tmp_path, monkeypatch):
    """Start the process-owned hosted-room service on a disposable temp home with atlas+ops."""

    class DurableRunStore:
        durable = True

    home = tmp_path / ".hermes"
    for profile in ("default", "atlas", "ops"):
        (home / "profiles" / profile).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(srv, "_run_idempotency_store", DurableRunStore(), raising=False)

    methods_groups.stop_hosted_room_service(timeout=1.0)
    service = methods_groups.start_hosted_room_service()
    assert service is not None
    # Deterministic transport + explicit local roster so validate_roster accepts atlas & ops.
    service.local_profiles = lambda: ("default", "atlas", "ops")
    service.rpc = _DeterministicRPC()
    service.runtime.rpc = service.rpc
    try:
        yield service
    finally:
        methods_groups.stop_hosted_room_service(timeout=1.0)


ATLAS_OPS = (
    RoomMember(member_id="atlas", profile="atlas", handle="atlas"),
    RoomMember(member_id="ops", profile="ops", handle="ops"),
)


def test_pilot_runs_full_atlas_ops_lifecycle_through_rpc_contract(isolated_service):
    """create -> send -> observe terminal member-turn, entirely via groups.* handlers."""
    receipt = run_room_pilot(
        srv,
        room_id="pilot-room-1",
        name="Atlas + Ops Pilot",
        members=ATLAS_OPS,
        text="@ops confirm the pilot control is wired",
        deadline_seconds=5.0,
        poll_interval_seconds=0.01,
    )

    # (1) fresh two-member atlas+ops hosted room
    assert receipt.room["room_id"] == "pilot-room-1"
    assert [m["member_id"] for m in receipt.room["members"]] == ["atlas", "ops"]
    assert receipt.room["idempotent"] is False
    assert receipt.room["authority_gateway_id"]

    # (2) deterministic message admitted
    assert receipt.sent_event["seq"] == 1
    assert receipt.sent_event["kind"] == "message.user"

    # (3) terminal member-turn observed through groups.state / groups.log
    assert receipt.terminal_kind == "turn.settled"
    assert receipt.terminal_kind in {f"turn.{s.split('.')[1]}" for s in TERMINAL_TURN_KINDS}
    log_kinds = [str(e["kind"]) for e in receipt.log_tail]
    assert log_kinds[:3] == ["message.user", "message.member", "turn.settled"]
    member_reply = next(e for e in receipt.log_tail if e["kind"] == "message.member")
    assert member_reply["payload"]["text"] == "reply from ops"

    summary = receipt.summary()
    assert summary["terminal_kind"] == "turn.settled"
    assert summary["member_ids"] == ["atlas", "ops"]
    assert summary["sent_event_seq"] == 1


def test_pilot_refuses_to_send_a_second_message(isolated_service):
    """Non-looping: run_room_pilot admits exactly one message per lifecycle."""
    run_room_pilot(
        srv, room_id="pilot-room-2", name="Once", members=ATLAS_OPS,
        text="@ops first", deadline_seconds=5.0, poll_interval_seconds=0.01)
    # The deterministic transport records exactly one submitted member turn (the addressed
    # member), carrying the user's message inside the composed Discussion prompt — one lifecycle,
    # one member turn, no loop.
    assert len(isolated_service.rpc.prompts) == 1
    profile, prompt = isolated_service.rpc.prompts[0]
    assert profile == "ops"
    assert "@ops first" in prompt


# --------------------------------------------------------------------------- stub-server unit tests


class _StubServer:
    """Minimal server exposing a scripted ``_methods`` registry for observer-level tests."""

    def __init__(self, handlers) -> None:
        self._methods = handlers


def _ok(rid, result):
    return {"result": result}


def _err(rid, code, message):
    return {"error": {"code": code, "message": message}}


def test_observe_captures_live_turn_then_terminal():
    """queued/running is surfaced before the terminal turn appears in the log."""
    state_seq = iter([
        {"room": {"room_id": "r"}, "driver_status": {"working": True, "counts": {"queued": 1}}},
        {"room": {"room_id": "r"}, "driver_status": {"working": True, "counts": {"running": 1}}},
        {"room": {"room_id": "r"}, "driver_status": {"working": False, "counts": {}}},
    ])
    log_seq = iter([
        {"events": [{"kind": "message.user", "seq": 1}]},
        {"events": [{"kind": "message.user", "seq": 1}]},
        {"events": [
            {"kind": "message.user", "seq": 1},
            {"kind": "message.member", "seq": 2, "payload": {"text": "reply from ops"}},
            {"kind": "turn.settled", "seq": 3, "event_id": "dterminal:x"}]},
    ])
    server = _StubServer({
        "groups.state": lambda rid, params: _ok(rid, next(state_seq)),
        "groups.log": lambda rid, params: _ok(rid, next(log_seq)),
    })
    pilot = RoomPilot(server, poll_interval_seconds=0, sleep=lambda _s: None)
    observed, terminal, events, polls, elapsed = pilot.observe_until_terminal(
        room_id="r", deadline_seconds=5.0, max_polls=10)

    assert polls == 3
    assert terminal["kind"] == "turn.settled"
    assert any(s.get("working") for s in observed)
    assert any(s["counts"].get("running") for s in observed)


def test_observe_is_non_looping_and_times_out():
    """A room that never terminates raises RoomPilotTimeout inside the poll budget."""
    server = _StubServer({
        "groups.state": lambda rid, params: _ok(
            rid, {"room": {"room_id": "r"}, "driver_status": {"working": True, "counts": {"running": 1}}}),
        "groups.log": lambda rid, params: _ok(rid, {"events": [{"kind": "message.user", "seq": 1}]}),
    })
    ticks = iter([0.0] + [i * 0.001 for i in range(1, 50)])
    pilot = RoomPilot(
        server, poll_interval_seconds=0, clock=lambda: next(ticks), sleep=lambda _s: None)
    with pytest.raises(RoomPilotTimeout) as excinfo:
        pilot.observe_until_terminal(room_id="r", deadline_seconds=5.0, max_polls=7)
    assert excinfo.value.polls == 7
    assert excinfo.value.room_id == "r"


def test_run_pilot_refuses_when_driver_unavailable():
    """The control fails closed with a clear error when no hosted-room driver is advertised."""
    server = _StubServer({
        "groups.capabilities": lambda rid, params: _ok(rid, {"driver": False, "methods": []}),
    })
    with pytest.raises(RoomPilotError) as excinfo:
        run_room_pilot(
            server, room_id="r", name="n", members=ATLAS_OPS, text="hi",
            deadline_seconds=1.0, sleep=lambda _s: None)
    assert excinfo.value.code == 4123
    assert "driver is unavailable" in str(excinfo.value)


def test_run_pilot_propagates_handler_error_envelopes():
    """A rejecting handler surfaces as a RoomPilotError with its real code — never swallowed."""
    server = _StubServer({
        "groups.capabilities": lambda rid, params: _ok(rid, {"driver": True}),
        "groups.create": lambda rid, params: _err(rid, 4110, "room id was permanently retired"),
    })
    with pytest.raises(RoomPilotError) as excinfo:
        run_room_pilot(
            server, room_id="r", name="n", members=ATLAS_OPS, text="hi",
            deadline_seconds=1.0, sleep=lambda _s: None)
    assert excinfo.value.method == "groups.create"
    assert excinfo.value.code == 4110


def test_run_pilot_requires_two_members():
    server = _StubServer({})
    with pytest.raises(ValueError, match="at least two members"):
        run_room_pilot(
            server, room_id="r", name="n",
            members=(RoomMember("atlas", "atlas", "atlas"),), text="hi")


def test_pilot_rejects_a_server_without_a_method_registry():
    with pytest.raises(RoomPilotError, match="no registered _methods"):
        RoomPilot(SimpleNamespace())


def test_unknown_method_is_a_clear_error():
    pilot = RoomPilot(_StubServer({}))
    with pytest.raises(RoomPilotError, match="not registered"):
        pilot.state(room_id="r")
