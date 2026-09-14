# Governed mode — Release plan (later, separately-approved gateway restart)

Status at handoff: CODE + TESTS COMPLETE for Slip #004 remediation; READY FOR
SENTINEL RE-REVIEW. NOT independently approved yet — the prior Slip #002 SHA was
returned NEEDS_CHANGES by Hermes/Sentinel (dashboard governed bypass CRITICAL +
release-authority HIGH). NO restart, build, deploy, config rollout, or board
mutation was performed during this slip.

Branch: relay/slip004-governed-authority
Review SHA (this remediation): HEAD of branch relay/slip004-governed-authority
  (exact SHA reported in the delivery receipt; a commit cannot embed its own hash)
Reviewed-and-superseded base (NEEDS_CHANGES): 3565cbba9cacc73e5926312817aac3bbda7fd338
Original governed-mode base: 359190776094ec9f2e5ec16e18e9dc8d63c7864d
Re-review: PENDING — Sentinel under a new review slip after this handoff.

## What Slip #004 changed vs the NEEDS_CHANGES SHA
- Central governed authority boundary `assert_governed_transition_allowed()` in
  kanban_db.py, shared by every status surface. A governed unreleased task
  cannot enter ready/running/review, and no governed task reaches `done` outside
  the acceptance lifecycle.
- Dashboard `_set_status_direct` now calls the boundary (drag/PATCH/bulk);
  refusals surface as HTTP 409, raw state stays todo + execution_authorized=0.
- `complete_task` refuses an unreleased governed task at the DB boundary
  regardless of caller surface (dashboard "done", CLI, tool).
- `create_task` forces delivery_required for ALL governed work (internal AND
  conversation-origin), so internal governed work cannot skip acceptance.
- `release_task` is gated on a server-resolved caller identity
  (`resolve_release_authorities()`, fail-closed default {'hermes'} + board
  orchestrator_profile, widenable via `kanban.governed_release_authorities`);
  caller-supplied `actor` is audit provenance only and confers no authority.

## Authority model / trust boundary (BLOCKED-ARCHITECTURE — see GOVERNED_RELEASE_ARCHITECTURE_DECISION.md)
Slip #006 finding: the release control is NOT a security boundary. The board is
a same-user-writable SQLite file with no service/OS isolation, so (1) a raw
`sqlite3` write sets execution_authorized=1 bypassing all app logic, and (2)
`get_active_profile_name()` derives identity from caller-settable HERMES_HOME, so
a profile-home spoof forges the 'hermes' release authority. Both bypasses are
reproduced by strict-xfail tests in tests/hermes_cli/test_kanban_governed_mode.py.
An unforgeable release CANNOT be built in-process under this threat model.
Governed release/complete state guards from Slip #004 remain effective ONLY as
operational/audit controls against non-adversarial mistakes — they must NOT be
relied on as security. Do NOT enable governed mode on any board as a security
feature until a real boundary (memo Option A service-owned DB/ACL, or Option B)
ships, or Wessam explicitly accepts the same-user impersonation residual (Option
C). The internal-acceptance liveness route is intentionally NOT implemented yet
because it would inherit the forgeable authority.

## Proof no restart occurred during this slip
- No `hermes desktop --force-build`, `npm run build`, PM2 action, gateway
  stop/start, or `serve` restart was invoked. All work was file edits + reads +
  `scripts/run_tests.sh` in an ISOLATED git worktree
  (.worktrees/slip004-governed-authority), never the live checkout.
- The review worktree (.worktrees/slip002-governed-mode) is preserved untouched
  at 3565cbba... including its untracked `%SystemDrive%/` residue for audit.
- The running gateway/Desktop process tree is untouched; it still holds the
  pre-remediation sys.modules. The change takes effect ONLY after an approved
  restart (see below).

## Why a restart is eventually required (not now)
The kanban lifecycle (kanban_db, kanban_db_dispatch, kanban_decompose), the
dashboard plugin_api, and the gateway/tui acceptance ingress are imported into
the live gateway and Desktop `serve` backend at boot. A running gateway keeps
the OLD modules in sys.modules; new governed-mode behavior is inert until those
processes reload. Same mixed-sys.modules constraint noted for t_32b4f7a3.

## Restart scope (when Wessam approves, executed by Ops externally)
1. Integrate the approved SHA into the live checkout preserving unrelated dirty
   work (same guarded-worktree + patch pattern as t_32b4f7a3).
2. Package Desktop via the maintained path: `hermes desktop --force-build
   --build-only` (targets ONLY release-tree Hermes.exe; no PM2/fleet kill).
3. Relaunch the packaged app externally (`hermes desktop --skip-build`).
4. The messaging gateway (survives the app) must also be restarted so its
   sys.modules reload — restart only the lock-holder gateway from an Ops
   (non-Desktop-hosted) context; do NOT broad-kill the fleet.
5. Configure `kanban.governed_release_authorities` for the operator's release
   profile BEFORE enabling governed mode on any board (else release fails closed
   for every profile except 'hermes'/orchestrator_profile).

## Health checks after restart
- `hermes kanban ...` on a governed-flagged board: a governed create lands
  `todo` execution_authorized=0; `hermes kanban release <id>` from an authorized
  profile promotes to ready; an unauthorized profile's release is refused; a
  dispatcher tick does NOT claim it before release.
- Dashboard: dragging/PATCHing a governed unreleased task to ready/done returns
  409 and leaves raw state todo/0; a governed task reaches done only via the
  acceptance lifecycle by the named acceptance_owner.
- Governed review requires a distinct reviewer; governed triage is not
  auto-decomposed; a governed conversation task completes to delivery_pending,
  and only the named acceptance_owner finalizes.
- `gateway status` responds; the new gateway PID owns the dispatcher lock;
  gateway_state.json code_sha matches the built dist.

## Rollback
- Live app: existing `release/win-unpacked.bak` is restored automatically on a
  failed integrity-gated staged swap.
- Source: revert to base 359190776094ec9f2e5ec16e18e9dc8d63c7864d (the branch is
  additive; governed defaults 0 so reverting is safe — no data migration to
  undo; the added columns are inert for non-governed rows).
- If post-restart E2E fails: STOP, capture package hash/PIDs/logs, escalate to
  Hermes for a controlled rollback decision (do not hand-swap or broad-kill).

## Governance note
This slip authorized code + tests ONLY. The live-gateway restart, config
enabling of governed mode on any board, all-board migration, and any remote
push/PR/publish remain separately gated on Wessam's approval via Hermes.
