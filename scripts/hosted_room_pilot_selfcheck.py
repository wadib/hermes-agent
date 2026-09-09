"""Self-check: run the supported non-looping hosted-room pilot control end to end and print a
structured, read-back receipt. Uses an ISOLATED, DISPOSABLE service (a throwaway temp home + a
deterministic in-process transport) — it never touches The Council or the live gateway state, per
the task's scope. Real Pilot creation against the live gateway is left to the blocked parent task.

Run:  venv/Scripts/python.exe scripts/hosted_room_pilot_selfcheck.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _DeterministicRPC:
    """Same-gateway session transport that settles each member turn deterministically."""

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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hosted-room-pilot-selfcheck-"))
    home = tmp / ".hermes"
    for profile in ("default", "atlas", "ops"):
        (home / "profiles" / profile).mkdir(parents=True)
    os.environ["HERMES_HOME"] = str(home)

    import tui_gateway.server as srv
    from tui_gateway import methods_groups
    from tui_gateway.hosted_room_pilot import RoomMember, run_room_pilot

    class DurableRunStore:
        durable = True

    srv._run_idempotency_store = DurableRunStore()

    methods_groups.stop_hosted_room_service(timeout=1.0)
    service = methods_groups.start_hosted_room_service()
    if service is None:
        print("FAIL: could not start an isolated hosted-room service", file=sys.stderr)
        return 1
    service.local_profiles = lambda: ("default", "atlas", "ops")
    service.rpc = _DeterministicRPC()
    service.runtime.rpc = service.rpc

    try:
        caps = None
        from tui_gateway.hosted_room_pilot import RoomPilot
        caps = RoomPilot(srv).capabilities()
        print("=== groups.capabilities (isolated gateway) ===")
        print(json.dumps({
            "protocol_version": caps.get("protocol_version"),
            "driver": caps.get("driver"),
            "authority_gateway_id": caps.get("authority_gateway_id"),
            "methods": caps.get("methods"),
        }, indent=2))

        receipt = run_room_pilot(
            srv,
            room_id="selfcheck-atlas-ops",
            name="Atlas + Ops Pilot (self-check)",
            members=(
                RoomMember(member_id="atlas", profile="atlas", handle="atlas"),
                RoomMember(member_id="ops", profile="ops", handle="ops"),
            ),
            text="@ops confirm the non-looping pilot control drove a full member turn",
            deadline_seconds=10.0,
            poll_interval_seconds=0.02,
        )

        print("\n=== RoomPilotReceipt.summary() (live read-back) ===")
        print(json.dumps(receipt.summary(), indent=2))

        print("\n=== observed driver_status progression ===")
        print(json.dumps(receipt.observed_statuses, indent=2))

        print("\n=== groups.log tail (published room events) ===")
        print(json.dumps([
            {"seq": e.get("seq"), "kind": e.get("kind"),
             "text": (e.get("payload") or {}).get("text")}
            for e in receipt.log_tail], indent=2))

        ok = (
            receipt.room["room_id"] == "selfcheck-atlas-ops"
            and [m["member_id"] for m in receipt.room["members"]] == ["atlas", "ops"]
            and receipt.sent_event["seq"] == 1
            and receipt.terminal_kind == "turn.settled"
            and [e["kind"] for e in receipt.log_tail][:3]
            == ["message.user", "message.member", "turn.settled"])
        print("\nRESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        methods_groups.stop_hosted_room_service(timeout=2.0)


if __name__ == "__main__":
    raise SystemExit(main())
