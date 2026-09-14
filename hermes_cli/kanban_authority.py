"""Kanban governed-authority — LOCAL OPERATIONAL model (Slip #010).

Scope (Wessam, single-user trusted local computer): this control prevents
ACCIDENTAL or unapproved APPLICATION behavior — a secondary-profile agent or a
UI/tool path releasing or accepting governed work without the operator's
board-owner context making the decision. It is NOT a defense against a malicious
same-user administrator who deliberately raw-writes the SQLite board, retargets
files, or debugs processes; that is explicitly OUT OF SCOPE and documented as a
trusted-local limitation, not a defect or a security claim.

The authority is therefore a simple PROCESS-ROLE check, not a secret capability:
governed release / internal acceptance is an operational decision made only in
the trusted default-Hermes board-owner process (the process whose HERMES_HOME is
the default home, under which the shared board lives). Every other profile
gateway/agent may only submit a non-authoritative request.

Superseded (removed in Slip #010): the Slip #007/#009 secret-capability file,
minting, and constant-time verification — that machinery existed only for the
abandoned Option A OS-security direction and added no value under this model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def is_board_authority_owner(home: Optional[Path] = None) -> bool:
    """Whether THIS process is the trusted board-authority owner that may make
    the operational release/accept decision.

    True only when the process HERMES_HOME is the DEFAULT home — the shared board
    lives under the default tree, and the default-Hermes context is the single
    operational owner. Secondary profiles return False, so an ordinary profile
    agent/app path cannot release or accept; it may only request.

    Operational control, NOT a security boundary: on a single-user box a user who
    deliberately sets HERMES_HOME to the default could assume this role — that is
    the accepted trusted-local limitation, not a threat this guards against.
    """
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.profiles import _get_default_hermes_home
        return get_hermes_home().resolve() == _get_default_hermes_home().resolve()
    except Exception:
        return False
