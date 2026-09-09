"""Supported, non-looping operator control for the process-owned hosted-room RPC lifecycle.

Desktop's ``CreateGroupChatDialog.create()`` only mints a local ``roomId`` + bot ui_meta; it
never reaches the hosted-room driver. This module is the missing supported control: it drives
the SAME registered ``groups.*`` JSON-RPC handlers that the authenticated ``/api/ws`` transport
dispatches (``server._methods[name](rid, params)``), so it inherits the gateway's real trust
boundary. It NEVER extracts or forges a session token/ticket/internal credential and NEVER
writes hosted-room SQLite tables directly — every state change is a handler call, every read is
``groups.state`` / ``groups.log``.

"Non-looping" is a hard contract, not a hope: :func:`run_room_pilot` creates one fresh room,
admits exactly one deterministic message, then bounded-polls to a terminal member-turn under a
fixed poll budget AND a wall-clock deadline. There is no agent auto-continue loop and no
unbounded retry; when the budget is exhausted it raises :class:`RoomPilotTimeout` rather than
spinning. The control returns a structured :class:`RoomPilotReceipt` that captures the observed
``queued``/``running`` -> terminal progression for read-back evidence.

The control operates on whatever hosted-room service the process already owns. Callers that want
an isolated, disposable run (tests, self-check) pass their own started service via
``service=...``; the default binds and starts the process-owned service exactly as the gateway
lifespan does. Real Pilot creation against the live gateway is deliberately left to the blocked
parent task — this module's self-check runs only against an isolated temp fixture.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence

# Terminal member-turn event kinds published into the room log by the Discussion planner
# (see gateway/hosted_room_discussion.plan_publication -> ``turn.<status>``).
TERMINAL_TURN_KINDS = frozenset(
    {"turn.settled", "turn.failed", "turn.cancelled", "turn.deferred"})
# Statuses the driver reports for an in-flight member turn (gateway/hosted_room_driver).
LIVE_TURN_STATUSES = ("queued", "running", "stopping")

_DEFAULT_POLL_INTERVAL_SECONDS = 0.05
_DEFAULT_DEADLINE_SECONDS = 30.0
_DEFAULT_MAX_POLLS = 600


class RoomPilotError(RuntimeError):
    """A hosted-room RPC handler rejected a pilot step."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} failed [{code}]: {message}")
        self.method = method
        self.code = code


class RoomPilotTimeout(RoomPilotError):
    """The bounded observation budget was exhausted before a terminal member-turn."""

    def __init__(self, room_id: str, polls: int, elapsed: float) -> None:
        RuntimeError.__init__(
            self,
            f"room {room_id!r} did not reach a terminal member-turn within "
            f"{polls} polls / {elapsed:.2f}s")
        self.method = "groups.log"
        self.code = 0
        self.room_id = room_id
        self.polls = polls
        self.elapsed = elapsed


@dataclass(frozen=True)
class RoomMember:
    """One local (same-gateway) member of the pilot room."""

    member_id: str
    profile: str
    handle: str

    def as_param(self) -> dict[str, str]:
        return {"member_id": self.member_id, "profile": self.profile, "handle": self.handle}


@dataclass
class RoomPilotReceipt:
    """Verifiable record of one non-looping pilot lifecycle."""

    room_id: str
    room: Mapping[str, Any]
    sent_event: Mapping[str, Any]
    observed_statuses: list[dict[str, Any]] = field(default_factory=list)
    terminal_event: Mapping[str, Any] | None = None
    log_tail: list[Mapping[str, Any]] = field(default_factory=list)
    polls: int = 0
    elapsed_seconds: float = 0.0

    @property
    def saw_live_turn(self) -> bool:
        """Whether an intermediate queued/running/stopping turn was observed before terminal."""
        return any(
            any(int(snap.get("counts", {}).get(status, 0)) for status in LIVE_TURN_STATUSES)
            or snap.get("working") is True
            for snap in self.observed_statuses)

    @property
    def terminal_kind(self) -> str | None:
        return None if self.terminal_event is None else str(self.terminal_event.get("kind"))

    def summary(self) -> dict[str, Any]:
        """Compact, JSON-serializable view for operator read-back."""
        return {
            "room_id": self.room_id,
            "authority_gateway_id": self.room.get("authority_gateway_id"),
            "authority_epoch": self.room.get("authority_epoch"),
            "member_ids": [m.get("member_id") for m in (self.room.get("members") or [])],
            "sent_event_seq": self.sent_event.get("seq"),
            "saw_live_turn": self.saw_live_turn,
            "terminal_kind": self.terminal_kind,
            "terminal_event_id": None if self.terminal_event is None
            else self.terminal_event.get("event_id"),
            "log_kinds": [str(e.get("kind")) for e in self.log_tail],
            "polls": self.polls,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


class RoomPilot:
    """Drive the hosted-room lifecycle through the registered ``groups.*`` RPC handlers.

    ``dispatch`` is the supported call seam: ``server._methods[name](request_id, params)``.
    It is exactly what the authenticated ``/api/ws`` transport and the in-process driver use,
    so the control never bypasses the gateway's method contract or its credential boundary.
    """

    def __init__(
        self, server: ModuleType, *,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep) -> None:
        if not hasattr(server, "_methods"):
            raise RoomPilotError(
                "<init>", 0, "server module exposes no registered _methods registry")
        self._server = server
        self._rid = 0
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._clock = clock
        self._sleep = sleep

    def _call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        handler = self._server._methods.get(method)
        if handler is None:
            raise RoomPilotError(method, 0, "method is not registered on this gateway")
        self._rid += 1
        envelope = handler(f"pilot-{self._rid}", dict(params))
        if not isinstance(envelope, dict):
            raise RoomPilotError(method, 5000, "handler returned a non-envelope result")
        error = envelope.get("error")
        if isinstance(error, dict):
            raise RoomPilotError(
                method, int(error.get("code") or 5000),
                str(error.get("message") or "gateway rejected the request"))
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise RoomPilotError(method, 5000, "handler returned no result payload")
        return result

    def capabilities(self) -> dict[str, Any]:
        """Return the gateway's advertised hosted-room capabilities (driver readiness, methods)."""
        return self._call("groups.capabilities", {})

    def create_room(
        self, *, room_id: str, name: str, members: Sequence[RoomMember]) -> dict[str, Any]:
        result = self._call("groups.create", {
            "room_id": room_id, "name": name,
            "members": [m.as_param() for m in members]})
        return result["room"]

    def send(self, *, room_id: str, event_id: str, text: str, thread_id: str) -> dict[str, Any]:
        result = self._call("groups.send", {
            "room_id": room_id, "event_id": event_id,
            "payload": {"text": text, "thread_id": thread_id}})
        return result["event"]

    def state(self, *, room_id: str) -> dict[str, Any]:
        return self._call("groups.state", {"room_id": room_id})

    def log(self, *, room_id: str, since_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        return self._call(
            "groups.log", {"room_id": room_id, "since_seq": since_seq, "limit": limit})

    def _terminal_event(self, events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        for event in reversed(events):
            if str(event.get("kind")) in TERMINAL_TURN_KINDS:
                return event
        return None

    def observe_until_terminal(
        self, *, room_id: str, deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
        max_polls: int = _DEFAULT_MAX_POLLS) -> tuple[
            list[dict[str, Any]], Mapping[str, Any] | None, list[Mapping[str, Any]], int, float]:
        """Bounded poll of state+log until a terminal member-turn appears.

        Returns ``(observed_statuses, terminal_event, log_events, polls, elapsed)``. Raises
        :class:`RoomPilotTimeout` if neither the poll budget nor the deadline yields a terminal
        turn — the non-looping guarantee.
        """
        if deadline_seconds <= 0 or max_polls <= 0:
            raise ValueError("deadline_seconds and max_polls must be positive")
        observed: list[dict[str, Any]] = []
        started = self._clock()
        polls = 0
        while polls < max_polls:
            polls += 1
            state = self.state(room_id=room_id)
            driver_status = state.get("driver_status")
            if isinstance(driver_status, Mapping):
                observed.append({
                    "poll": polls,
                    "working": driver_status.get("working"),
                    "blocked": driver_status.get("blocked"),
                    "counts": dict(driver_status.get("counts") or {})})
            log = self.log(room_id=room_id)
            events = list(log.get("events") or [])
            terminal = self._terminal_event(events)
            elapsed = self._clock() - started
            if terminal is not None:
                return observed, terminal, events, polls, elapsed
            if elapsed >= deadline_seconds:
                break
            self._sleep(self._poll_interval_seconds)
        raise RoomPilotTimeout(room_id, polls, self._clock() - started)


def run_room_pilot(
    server: ModuleType, *,
    room_id: str, name: str, members: Sequence[RoomMember],
    text: str, thread_id: str = "pilot-thread-1", event_id: str = "pilot-user-1",
    deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
    max_polls: int = _DEFAULT_MAX_POLLS,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    require_driver: bool = True,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep) -> RoomPilotReceipt:
    """Run one non-looping create -> send -> observe-to-terminal lifecycle and return a receipt.

    ``require_driver`` refuses to start when the gateway advertises no hosted-room driver, so an
    operator gets a clear ``driver unavailable`` error instead of a room that can never make a
    member turn. The whole flow goes through the registered ``groups.*`` handlers.
    """
    if len(members) < 2:
        raise ValueError("a hosted room pilot requires at least two members")
    pilot = RoomPilot(
        server, poll_interval_seconds=poll_interval_seconds, clock=clock, sleep=sleep)
    caps = pilot.capabilities()
    if require_driver and not caps.get("driver"):
        raise RoomPilotError(
            "groups.capabilities", 4123,
            "hosted-room driver is unavailable; start the gateway worker before piloting a room")
    room = pilot.create_room(room_id=room_id, name=name, members=members)
    sent = pilot.send(room_id=room_id, event_id=event_id, text=text, thread_id=thread_id)
    observed, terminal, events, polls, elapsed = pilot.observe_until_terminal(
        room_id=room_id, deadline_seconds=deadline_seconds, max_polls=max_polls)
    return RoomPilotReceipt(
        room_id=room_id, room=room, sent_event=sent, observed_statuses=observed,
        terminal_event=terminal, log_tail=events, polls=polls, elapsed_seconds=elapsed)
