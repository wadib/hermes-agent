"""Canonical mutation-lease authority for Kanban task writes.

The board DB is shared by workers, CLI, dashboard, desktop and gateway.  A worker
cannot establish authority by naming a task: its dispatcher-issued run and claim
lock must match the durable task row before it can obtain a lease.  Public task
mutators consult the context-local authority, while legacy operator paths remain
usable when no live lease exists.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional


@dataclass(frozen=True)
class TaskMutationAuthority:
    task_id: str
    workspace_key: str
    session_root: str
    holder: str
    fence: int


class MutationLeaseBusyError(RuntimeError):
    """Another live mutation authority owns a shared Kanban mutation domain."""


class MutationLeaseLostError(RuntimeError):
    """A stale/fenced authority attempted a task mutation."""


_MUTATION_AUTHORITY: contextvars.ContextVar[Optional[TaskMutationAuthority]] = contextvars.ContextVar(
    "kanban_task_mutation_authority", default=None
)


def _workspace_key(task: Any) -> str:
    """Canonical persisted workspace identity without materialising a workspace.

    Resolving only the stored path is intentional: a guard must not create a
    worktree/scratch directory merely to decide whether it may mutate a row.
    """
    raw = str(getattr(task, "workspace_path", "") or "").strip()
    if not raw:
        return f"task:{task.id}"
    try:
        return str(Path(raw).expanduser().resolve(strict=False))
    except OSError:
        return raw


def _session_root(task: Any) -> str:
    """Use the durable task session link; task id is the isolated legacy fallback."""
    return str(getattr(task, "session_id", "") or "").strip() or f"task:{task.id}"


def task_mutation_scopes(task: Any) -> tuple[str, str]:
    """Canonical workspace and root-session identities for a persisted task."""
    return _workspace_key(task), _session_root(task)


def _scope_rows(conn, task_id: str, workspace_key: str, session_root: str):
    scopes = (
        ("task", str(task_id)),
        ("workspace", workspace_key),
        # Root-session authority is scoped to the workspace.  A single user
        # session may legitimately own concurrent isolated worktrees.
        ("workspace_session", f"{workspace_key}\0{session_root}"),
    )
    rows = conn.execute(
        "SELECT scope_kind, scope_key, holder, fence, expires_at FROM task_mutation_leases "
        "WHERE (scope_kind, scope_key) IN ((?, ?), (?, ?), (?, ?))",
        tuple(value for scope in scopes for value in scope),
    ).fetchall()
    return scopes, {(row["scope_kind"], row["scope_key"]): row for row in rows}


def acquire_task_mutation_authority(conn, task_id: str, *, holder: str, ttl_seconds: int = 60) -> Optional[TaskMutationAuthority]:
    """Acquire authority from a durable task row, never caller-supplied domains."""
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    if task is None:
        return None
    workspace_key, session_root = task_mutation_scopes(task)
    lease = kb.acquire_mutation_lease(
        conn, task_id, workspace_key=workspace_key, session_root=session_root,
        holder=holder, ttl_seconds=ttl_seconds,
    )
    if lease is None:
        return None
    return TaskMutationAuthority(task.id, workspace_key, session_root, lease.holder, lease.fence)


def acquire_worker_mutation_authority(
    conn, task_id: str, *, claim_lock: str, expected_run_id: Optional[int], ttl_seconds: int = 60,
) -> Optional[TaskMutationAuthority]:
    """Acquire a worker authority only after verifying the dispatcher claim/run.

    ``claim_lock`` is an opaque dispatcher capability.  It is compared to the
    current durable row and is never accepted as an identity by itself.
    """
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    claim_lock = str(claim_lock or "").strip()
    # Older dispatcher workers did not receive the run/claim env pins.  Derive
    # those opaque values only from the durable claimed row for that narrow
    # compatibility path; never accept an arbitrary supplied replacement.
    if task is not None and not claim_lock:
        claim_lock = task.claim_lock or ""
        if expected_run_id is None:
            expected_run_id = task.current_run_id
    if (
        task is None
        or task.status != "running"
        or not claim_lock
        or task.claim_lock != claim_lock
        or expected_run_id is None
        or task.current_run_id != int(expected_run_id)
    ):
        return None
    holder = f"run:{task.current_run_id}:claim:{claim_lock}"
    return acquire_task_mutation_authority(conn, task_id, holder=holder, ttl_seconds=ttl_seconds)


@contextlib.contextmanager
def mutation_authority(authority: TaskMutationAuthority) -> Iterator[TaskMutationAuthority]:
    """Install verified authority for nested DB mutation calls in this execution."""
    token = _MUTATION_AUTHORITY.set(authority)
    try:
        yield authority
    finally:
        _MUTATION_AUTHORITY.reset(token)


def current_mutation_authority() -> Optional[TaskMutationAuthority]:
    return _MUTATION_AUTHORITY.get()


def assert_task_mutation_allowed(conn, task_id: str) -> None:
    """Fence every guarded task write.

    A matching context must retain every canonical scope and fence.  Without a
    context, legacy CLI/dashboard/gateway operator writes remain valid only if
    no live worker authority owns this task/workspace/session domain.  This
    preserves historical migrations and human recovery while steering competing
    writers before their mutation reaches SQLite.
    """
    from hermes_cli import kanban_db as kb
    import time

    task = kb.get_task(conn, task_id)
    if task is None:
        return
    workspace_key, session_root = task_mutation_scopes(task)
    scopes, rows = _scope_rows(conn, task.id, workspace_key, session_root)
    now = int(time.time())
    authority = _MUTATION_AUTHORITY.get()
    if authority is not None and authority.task_id == task.id:
        if (
            authority.workspace_key != workspace_key
            or authority.session_root != session_root
            or not kb.mutation_lease_valid(conn, task.id, holder=authority.holder, fence=authority.fence)
        ):
            raise MutationLeaseLostError(f"mutation lease lost for {task.id}")
        for scope in scopes:
            row = rows.get(scope)
            if row is None or row["holder"] != authority.holder or int(row["fence"]) != authority.fence or int(row["expires_at"]) < now:
                raise MutationLeaseLostError(f"mutation lease lost for {task.id}")
        return
    foreign = [row for row in rows.values() if int(row["expires_at"]) >= now]
    if foreign:
        raise MutationLeaseBusyError(
            f"mutation lease busy for {task.id}; wait for the active worker or reclaim its expired/dead claim"
        )


def guard_task_mutator(fn):
    """Wrap a public ``(conn, task_id, ...)`` domain mutator once at module load."""
    import functools

    @functools.wraps(fn)
    def guarded(conn, task_id, *args, **kwargs):
        assert_task_mutation_allowed(conn, task_id)
        return fn(conn, task_id, *args, **kwargs)

    return guarded


def guard_link_mutator(fn):
    """A dependency edge mutates both its parent and child task domains."""
    import functools

    @functools.wraps(fn)
    def guarded(conn, parent_id, child_id, *args, **kwargs):
        assert_task_mutation_allowed(conn, parent_id)
        assert_task_mutation_allowed(conn, child_id)
        return fn(conn, parent_id, child_id, *args, **kwargs)

    return guarded
