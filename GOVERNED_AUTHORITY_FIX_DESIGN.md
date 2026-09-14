# Slip #004 — Governed authority-boundary remediation (design)

Worktree: C:\Users\DELL\AppData\Local\hermes\hermes-agent\.worktrees\slip004-governed-authority
Branch: relay/slip004-governed-authority  Base/reviewed SHA: 3565cbba9cacc73e5926312817aac3bbda7fd338
Scope: code + tests only. No remote/push/PR, no restart/build/deploy, no live board/config mutation.
Test interpreter: HERMES_PYTHON=C:/Users/DELL/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe
  (venv has concurrent_log_handler/fastapi/starlette/pytest — the deps Sentinel's WindowsApps python3 lacked, which blocked gateway/TUI collection).

## Root cause (Sentinel #003)
Governance was enforced only at the paths Slip #002 enumerated (recompute_ready, promote_task,
claim, review lane, decompose, gateway/desktop acceptance). It was NOT enforced at:
  (a) the dashboard direct-status surface (`_set_status_direct`, `_STATUS_HANDLERS`), which raw-writes
      todo->ready with only a parent check and routes done->complete_task; and
  (b) `complete_task` itself, which accepts `ready` and has no governed/execution_authorized guard; and
  (c) `release_task`, which trusts a caller-supplied `actor` string with no authority gate; and
  (d) `create_task`, which forces delivery_required only when session_id/creator_task_id present, so an
      INTERNAL governed task skips delivery/acceptance and can reach done directly.
Fix principle: enforce the governed invariant at the DB/lifecycle AUTHORITY BOUNDARY, so it holds
regardless of entry surface — not per-path patches.

## Fix 1 (CRITICAL) — central governed-transition guard at the DB boundary
Add `_assert_governed_transition_allowed(conn, task_id, target_status, *, via)` in kanban_db.py, the
single chokepoint every status-changing lifecycle op calls BEFORE the UPDATE. Rule for a governed task:
  - target in {ready, running, review}: allowed only if execution_authorized=1 (i.e. released). Else raise
    GovernedTransitionError (todo/execution_authorized stay unchanged).
  - target in {delivery_pending, awaiting_acceptance}: only via the trusted delivery/acceptance ops
    (complete_task's delivery path / accept_delivery_*), never a direct status write.
  - target == done: only via the governed acceptance lifecycle (accept_delivery_from_gateway/desktop).
    complete_task on a governed task lands delivery_pending, never done.
Wire the guard into: complete_task (reject governed direct-done + governed-unreleased), _set_status_direct
(dashboard drag/direct), and confirm the structured verbs (request_review already guards distinct reviewer;
block/schedule/unblock/reopen/archive are non-executable or already governed-held via
_landing_status_after_parents). Dashboard maps GovernedTransitionError -> 409 (or 403) so PATCH/bulk/drag
fail clearly and raw state remains todo + execution_authorized=0.
New exception `GovernedTransitionError(ValueError)` so existing dashboard `_value_error_400`/`_map_errors`
can map it; choose 409 (state-conflict) as the primary code per the spec's 400/403/409 set.

## Fix 2 (HIGH) — release authority
`release_task` must not trust arbitrary `actor` audit text. Add a durable, server-resolved release-authority
policy:
  - Config key `kanban.governed_release_authorities` (list of profile names) + implicit default: the
    board's `orchestrator_profile` and the acceptance-owner default ('hermes'). If unset, default to
    {'hermes'} so the shipped behavior is fail-closed to the governance owner.
  - `release_task(conn, task_id, *, actor, reason, caller_identity=None)`: `caller_identity` is resolved by
    the trusted ingress (CLI resolves it from the active profile via get_active_profile_name, NOT from a
    caller-supplied string). Reject when caller_identity is None/not in the authority set, or when the call
    is a delegated-child context (`_assert_not_delegated_child_mutation`). Preserve `actor` as audited
    provenance only.
  - HONEST trust-limitation note (documented in code + receipt): on a local single-user install, profile
    identity is process/home-derived, not cryptographic; this enforces "only configured release-authority
    profiles" but cannot defend against a local user who can set HERMES_HOME/env. State this plainly; do
    not claim cryptographic security.
  - Idempotent: a second release on an already-released task returns (True, None) and writes NO duplicate
    `released` event (already the case; add a test).

## Fix 3 — governed completion requires acceptance (internal + conversation)
In create_task, a governed task is delivery_required regardless of session_id/creator_task_id (internal
governed work included). complete_task already routes delivery_required -> delivery_pending; combined with
Fix 1 (governed target==done only via acceptance lifecycle) this closes direct-done for ALL governed work.
Internal governed work still requires the named acceptance_owner (default 'hermes'); the acceptance-owner
enforcement added in Slip #002 (accept_delivery_from_gateway/desktop) already gates the finalize.
Delivery-required needs a durable attachment before pending — for internal governed work with no artifact,
document that completion supplies one via kanban_complete(artifacts=...) or the task stays running; do NOT
invent a direct-done bypass.

## Fix 4 — correct GOVERNED_MODE_RELEASE_PLAN.md
Remove "independently APPROVED" and the stale 9270... "final commit". State: current status NEEDS_CHANGES
remediated on branch relay/slip004-governed-authority at <new SHA after commit>; Sentinel re-review pending;
no restart performed. Preserve the no-live-restart gate.

## Tests (RED on 3565cbba, GREEN on fix) — ported into repo modules
- tests/plugins/test_kanban_dashboard_plugin.py (or a new governed dashboard test): governed unreleased task
  PATCH ready -> 409; PATCH done -> 409; bulk + drag same; archive/reopen aliases cannot reach
  executable/final; final raw row {status:'todo', execution_authorized:0}. (ports Sentinel spec 1)
- tests/hermes_cli/test_kanban_governed_mode.py: complete_task on governed-unreleased and governed-unaccepted
  rejected at DB boundary regardless of caller; release rejects untrusted profile, trusted authority releases
  once, spoofed actor cannot bypass caller_identity, concurrent/idempotent second release => no duplicate
  event; internal governed + conversation governed both require named acceptance owner. (ports Sentinel spec 2)
- Re-run: focused governed suite + dashboard API + CLI release + delivery acceptance + notify/TUI +
  dispatch/reclaim + decompose + migration/back-compat. Baseline-compare all failures (known 3 env baselines).
