"""Slip #007 — server-owned governed-release capability.

The Kanban board is a same-user-writable SQLite file (Slip #006), so an
application check keyed on caller identity (profile/HERMES_HOME) is forgeable.
This module moves governed-release authority to a CAPABILITY held only by the
trusted board service (the gateway / ``hermes serve`` process): a random secret
minted at service boot, kept in memory for that process and written to a
capability file with restrictive permissions.

HONEST TRUST BOUNDARY (see GOVERNED_SERVICE_BOUNDARY_DESIGN.md):
- The capability makes release authority depend on POSSESSION OF THE SECRET,
  not on a caller-settable identity string. An ordinary agent that only knows a
  profile name cannot release.
- It becomes a real SECURITY boundary ONLY when the capability file and the
  board DB are ACL-restricted to a separate service account (OPS #007-OPS). On a
  single-user box with no ACL, a same-user process can still read the capability
  file or raw-write the DB — that residual is UNKNOWN/unresolved here and is NOT
  claimed as secured. This module never asserts otherwise.

No secret is logged. Values are compared in constant time.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Optional

_IS_WINDOWS = sys.platform == "win32"

# Process-local marker: set ONLY by the trusted service at boot (mint_service_
# capability). A bare CLI/agent process cannot flip this for itself without also
# possessing the secret, so is_service_context() cannot be self-asserted.
_IN_PROCESS_CAPABILITY: Optional[str] = None

_CAPABILITY_FILENAME = "kanban_release.cap"


def capability_path(home: Optional[Path] = None) -> Path:
    """Path of the service capability file for ``home`` (default: HERMES_HOME).

    Under ``<home>/kanban/`` beside the board so the same ACL that protects the
    board (OPS #007-OPS) protects the capability.
    """
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return Path(home) / "kanban" / _CAPABILITY_FILENAME


def mint_service_capability(home: Optional[Path] = None) -> str:
    """Called by the trusted board service at boot. Generate (or reuse) the
    capability secret, hold it in-process, and persist it 0o600. Returns the
    secret so the same process can pass it to :func:`release_task`.

    Idempotent within a boot: if a capability file already exists and is
    readable, adopt it (so a restart of the same service keeps working) rather
    than orphaning in-flight clients.
    """
    global _IN_PROCESS_CAPABILITY
    path = capability_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_capability_file(path)
    token = existing or secrets.token_hex(32)
    if existing is None:
        # Write with a restrictive mode from creation. On POSIX, 0o600; on
        # Windows the mode bits are advisory (real protection is the OPS #007
        # ACL) — we still set them so the intent is explicit and testable.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode("ascii"))
        finally:
            os.close(fd)
        if not _IS_WINDOWS:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    _IN_PROCESS_CAPABILITY = token
    return token


def _read_capability_file(path: Path) -> Optional[str]:
    try:
        data = path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None
    return data or None


def in_process_capability() -> Optional[str]:
    """The capability THIS process holds because it minted it (trusted service),
    or None. A bare agent/CLI process returns None."""
    return _IN_PROCESS_CAPABILITY


def is_service_context() -> bool:
    """True only when this process minted the capability (trusted board service).
    Cannot be self-asserted by an ordinary agent without the secret."""
    return _IN_PROCESS_CAPABILITY is not None


def verify_capability(candidate: Optional[str], home: Optional[Path] = None) -> bool:
    """Constant-time check that ``candidate`` matches the live capability.

    Match order: the in-process secret (trusted service performing the release
    itself), else the persisted capability file (a client that legitimately
    obtained it from the service). Fail closed on any miss/empty. Never logs the
    secret.
    """
    cand = (candidate or "").strip()
    if not cand:
        return False
    truth = _IN_PROCESS_CAPABILITY or _read_capability_file(capability_path(home))
    if not truth:
        return False
    return hmac.compare_digest(cand, truth)


def _reset_for_tests() -> None:
    """Test-only: clear the in-process capability marker."""
    global _IN_PROCESS_CAPABILITY
    _IN_PROCESS_CAPABILITY = None
