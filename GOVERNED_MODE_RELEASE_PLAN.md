# Slip #002 — Release plan (later, separately-approved gateway restart)

Status at handoff: CODE + TESTS COMPLETE and independently APPROVED. NO restart,
build, deploy, config rollout, or board mutation performed during this slip.

Branch: relay/slip002-governed-mode
Final commit: 9270fe9db81f0e7c2e602f35292c21ae8cf30b1b (fork/relay/slip002-governed-mode, remote verified equal)
Base: 359190776094ec9f2e5ec16e18e9dc8d63c7864d

## Proof no restart occurred during this slip
- No `hermes desktop --force-build`, `npm run build`, PM2 action, gateway
  stop/start, or `serve` restart was invoked. All work was file edits + reads +
  `scripts/run_tests.sh` in an ISOLATED git worktree
  (.worktrees/slip002-governed-mode), never the live checkout.
- The running gateway/Desktop process tree is untouched; it still holds the
  pre-Slip-#002 sys.modules. The change takes effect ONLY after an approved
  restart (see below).

## Why a restart is eventually required (not now)
The kanban lifecycle (kanban_db, kanban_db_dispatch, kanban_decompose) and the
gateway/tui acceptance ingress are imported into the live gateway and Desktop
`serve` backend at boot. A running gateway keeps the OLD modules in sys.modules;
new governed-mode behavior is inert until those processes reload. This is the
same mixed-sys.modules constraint noted for t_32b4f7a3.

## Restart scope (when Wessam approves, executed by Ops externally)
1. Integrate the approved SHA into the live checkout preserving unrelated dirty
   work (same guarded-worktree + patch pattern as t_32b4f7a3).
2. Package Desktop via the maintained path: `hermes desktop --force-build
   --build-only` (targets ONLY release-tree Hermes.exe; no PM2/fleet kill).
3. Relaunch the packaged app externally (`hermes desktop --skip-build`).
4. The messaging gateway (survives the app) must also be restarted so its
   sys.modules reload — restart only the lock-holder gateway from an Ops
   (non-Desktop-hosted) context; do NOT broad-kill the fleet.

## Health checks after restart
- `hermes kanban ...` on a governed-flagged board: a governed create lands
  `todo` execution_authorized=0; `hermes kanban release <id>` promotes to ready;
  a dispatcher tick does NOT claim it before release.
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
enabling of governed mode on any board, and all-board migration remain
separately gated on Wessam's approval via Hermes.
