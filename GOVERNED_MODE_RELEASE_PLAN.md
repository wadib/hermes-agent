# Governed mode — Release plan (later, separately-approved gateway restart)

Status: CODE + TESTS COMPLETE for Slip #009 (remove confused deputy; server-
internal governed authority). READY FOR SENTINEL RE-REVIEW. NOT independently
approved. NO restart, build, deploy, config rollout, board mutation, live
account/service/ACL, or remote action was performed.

Slip: #009 — Remove confused deputy; server-internal governed authority.
Branch: relay/slip009-remove-confused-deputy
Review SHA (this remediation): HEAD of branch relay/slip009-remove-confused-deputy
  (exact SHA reported in the delivery receipt; a commit cannot embed its own hash)
Base (reviewed NEEDS_CHANGES, Slip #008 confused-deputy HIGH): 2ad329078f866b2ce4caeefb6a8d0ebce0f6b105
Re-review: PENDING — Sentinel, on the exact new SHA, after this handoff.

## Lineage (for audit only — do not target these for approval/rollback)
Slip #002 governed mode -> #004 authority boundary -> #006 BLOCKED-ARCHITECTURE
memo -> #007 capability vertical slice (introduced the confused deputy) -> #008
review found the deputy HIGH -> #009 (this) removes it. The ONLY branch/SHA to
review or roll back is this Slip #009 branch at the SHA in the receipt.

## What Slip #009 changed vs the NEEDS_CHANGES SHA (2ad329)
- REMOVED the privileged `governed-release` / `governed-accept-internal` verbs
  from the generic gateway control socket (gateway/run.py, gateway/control_socket.py).
  A raw JSON request for either is now an unknown verb -> rejected, zero mutation.
  This closes the Slip #008 confused-deputy: no unauthenticated local caller can
  make the gateway spend its capability.
- Privileged release + internal acceptance are SERVER-INTERNAL only
  (kanban_db.release_task / accept_internal_governed_delivery gated by the
  in-process capability). No local verb exposes them.
- SINGLE board-authority owner: kanban_authority.should_mint_board_authority()
  mints/holds the capability ONLY in the default-home board-owner process; every
  secondary profile gateway holds no authority (fixes the Slip #007 over-mint
  where every gateway was a deputy).
- Ordinary CLI/agents get a durable NON-AUTHORITATIVE request only
  (kanban_db.submit_release_request -> `release_requested` event); it never
  changes execution_authorized/status and cannot auto-approve. The board-owner
  resolves requests separately from persisted policy; caller fields are audit
  provenance, never authority.
- Internal no-session acceptance still completes through the trusted internal
  path; missing owner/capability fails before mutation (no strand, no false Done).

## Authority model / trust boundary (honest — OPS #007-OPS still pending)
Release/accept authority = possession of the server-internal capability, held by
the single board-owner process. What is REAL and tested: the confused-deputy
socket surface is gone; a non-owner can only request; only the owner-context
capability path executes; internal acceptance is owner + capability gated.
What remains UNKNOWN / OPS #007-OPS: on a single-user box with no separate
Windows service identity + ACL, "board owner" is a process-role convention, not
an OS-enforced identity — a hostile same-user process could still set HERMES_HOME
to the default home to become "owner", RAW-WRITE the board DB, or read the
capability file. The capability/role becomes a real SECURITY boundary ONLY once
the board DB + capability file are ACL-restricted to a dedicated Windows service
account per the Ops #007-OPS design (virtual account NT SERVICE\HermesBoard,
ACL'd named pipe, direct-write denial). NO ACL/live proof exists yet; the
raw-DB-write residual is asserted positively in tests as an unresolved gap.

## Proof no restart / no live action occurred during this slip
- No `hermes desktop --force-build`, `npm run build`, PM2 action, gateway
  stop/start, `serve` restart, account/service/ACL creation, or DB migration.
  All work = file edits + reads + `scripts/run_tests.sh` in the isolated worktree
  (.worktrees/slip009-remove-confused-deputy), never the live checkout/board.
- Prior worktrees preserved untouched (slip007 2ad329, slip006 5d28f31e,
  slip004 65542e1d, slip002 3565cbba).
- The running gateway/Desktop tree is untouched; changes are inert until an
  approved restart reloads sys.modules.

## Restart scope (when Wessam approves, executed by Ops externally)
1. Integrate the approved SHA into the live checkout preserving unrelated dirty
   work (guarded-worktree + patch pattern).
2. Package Desktop via `hermes desktop --force-build --build-only` (release-tree
   Hermes.exe only; no PM2/fleet kill).
3. Relaunch the packaged app externally (`hermes desktop --skip-build`).
4. Restart the lock-holder gateway from an Ops (non-Desktop-hosted) context so
   its sys.modules reload; do NOT broad-kill the fleet.
5. The OS ACL / Windows service-account cutover (Ops #007-OPS) is a SEPARATE,
   separately-approved step with its own §9 shadow tests — NOT part of this slip
   and NOT to be bundled into the restart.

## Health checks after restart
- `hermes kanban release <id>` from a non-owner profile RECORDS a request and
  does NOT authorize; from the board-owner context it releases.
- A raw JSON `governed-release`/`governed-accept-internal` to the gateway control
  socket returns unknown-verb; state unchanged.
- Governed create lands todo/execution_authorized=0; dashboard drag/PATCH/bulk to
  ready/done on an unreleased governed task returns 409; internal no-session
  governed work reaches done only via the trusted owner acceptance path.
- `gateway status` responds; new gateway PID owns the dispatcher lock;
  gateway_state.json code_sha matches the built dist.

## Rollback
- Live app: existing `release/win-unpacked.bak` restored automatically on failed
  integrity-gated staged swap.
- Source: revert to base 2ad329078f866b2ce4caeefb6a8d0ebce0f6b105 (this slip is
  additive; governed defaults 0; the new event/request rows are inert for
  non-governed work — no data migration to undo).
- If post-restart E2E fails: STOP, capture package hash/PIDs/logs, escalate to
  Hermes for a controlled rollback decision (no hand-swap/broad-kill).

## Governance note
This slip authorized code + tests ONLY. The live-gateway restart, the Windows
service-account/ACL cutover, config enabling of governed mode on any board,
all-board migration, and any remote push/PR/publish remain separately gated on
Wessam's approval via Hermes. No ACL/live-identity security is claimed here.
