# Governed mode — Release plan (LOCAL OPERATIONAL scope, Slip #010)

Status: CODE + TESTS COMPLETE for Slip #010 (resume original Council reliability
under the LOCAL OPERATIONAL threat model). READY FOR HERMES TECHNICAL ACCEPTANCE.
NOT live. No restart/build/deploy, no board/config mutation, no account/service/
ACL/DB migration, no remote action was performed.

Slip: #010 — Local operational governance (Option A OS-isolation STOPPED).
Branch: relay/slip010-local-operational-governance
Review SHA: HEAD of branch relay/slip010-local-operational-governance
  (exact SHA in the delivery receipt; a commit cannot embed its own hash)
Base: 9135f1b0dd0ad4ab4587d97d8c0d5e8c39091906 (Slip #009 candidate baseline)
Review method: one fresh temporary read-only code-review helper (NOT Sentinel;
  Sentinel is external/customer-facing security only). Prior Sentinel findings are
  historical evidence only.

## Threat model (Wessam, explicit)
This is a trusted single-user local computer. The Council/governed control's job
is to prevent ACCIDENTAL or unapproved APPLICATION behavior by agents and UI/tool
paths. It does NOT defend against a malicious same-user administrator who
deliberately raw-writes the SQLite board, retargets OS files, or debugs
processes. Raw same-user SQLite/file/process tampering is OUT OF SCOPE and is a
documented trusted-local limitation — not a defect, not an unresolved security
gap, and no security is claimed against it.

## What Slip #010 changed vs base 9135f1b0
- Simplified `hermes_cli/kanban_authority.py` to a single process-ROLE check
  `is_board_authority_owner()` (default-home process = the operational board
  owner). REMOVED the Slip #007/#009 secret-capability machinery (mint, 0o600
  capability file, constant-time verify, in_process_capability) — it existed only
  for the abandoned Option A OS-security direction and added no value here.
- `release_task(..., authorized: bool)`: governed release requires the board-owner
  operational decision (`authorized=True`), not a secret and not caller identity.
  caller_identity/actor stay audit provenance only. Delegated children rejected.
- `accept_internal_governed_delivery(..., authorized: bool)`: same board-owner
  operational gate; internal no-session governed work still completes end-to-end
  (delivery_pending -> done by the named owner) and never strands.
- CLI `release`: board-owner releases directly; a non-owner submits a durable,
  NON-AUTHORITATIVE request (`submit_release_request` -> `release_requested`
  event) that never changes state and cannot auto-approve.
- gateway/run.py: removed the capability mint at control-socket start (nothing to
  mint). The Slip #008 confused-deputy fix stands — NO privileged release/accept
  verb is exposed on the generic control socket.
- Removed Option A docs: GOVERNED_RELEASE_ARCHITECTURE_DECISION.md,
  GOVERNED_SERVICE_BOUNDARY_DESIGN.md, OPS_007_DESIGN_INTEGRATION.md (they existed
  solely for the abandoned OS-security direction).

## In-scope invariants (all reproved by focused tests)
1. Governed mode opt-in; non-governed legacy unchanged.
2. Governed tasks do not auto-promote/decompose/reassign/review-dispatch.
3. Dashboard/CLI/tool/direct application lifecycle paths cannot ready/run/done
   governed work before explicit release + named acceptance.
4. Release request is non-authoritative and cannot auto-approve; the trusted
   default-Hermes board-owner makes the operational decision; profile/actor audit
   only.
5. Conversation-origin tasks have durable delivery binding/receipt; internal
   no-session tasks have a usable durable acceptance; executor cannot self-accept
   through normal app/tool surfaces.
6. Review requires a distinct reviewer and no same-profile auto-redispatch.
7. No privileged release/accept verbs on the generic control socket.
8. Raw same-user SQLite/file/process tampering OUT OF SCOPE, documented as a
   trusted-local limitation (test documents it as such, not as a defect).

## Proof no live action occurred
- No `hermes desktop --force-build`, `npm run build`, PM2 action, gateway/serve
  restart, account/service/ACL creation, or DB migration. All work = file edits +
  reads + `scripts/run_tests.sh` in the isolated worktree
  (.worktrees/slip010-local-operational-governance), never the live checkout/board.
- Prior worktrees preserved untouched (slip009 9135f1b0, slip007 2ad329,
  slip006 5d28f31e, slip004 65542e1d, slip002 3565cbba).

## Restart scope (when Wessam approves, executed by Ops externally)
1. Integrate the accepted SHA into the live checkout preserving unrelated dirty
   work (guarded-worktree + patch pattern).
2. Package Desktop via `hermes desktop --force-build --build-only`; relaunch the
   packaged app externally (`hermes desktop --skip-build`).
3. Restart the lock-holder gateway from an Ops (non-Desktop) context so its
   sys.modules reload; do NOT broad-kill the fleet.
No OS ACL / Windows service-account work is part of this or any follow-up — that
direction is stopped.

## Health checks after restart
- `hermes kanban release <id>` from a non-owner profile records a request and
  does NOT authorize; from the board-owner (default) context it releases.
- Governed create lands todo/execution_authorized=0; dashboard drag/PATCH/bulk to
  ready/done on an unreleased governed task returns 409; internal no-session
  governed work reaches done only via the board-owner acceptance path.
- A raw JSON governed-release/accept to the control socket returns unknown-verb.

## Rollback
- Live app: existing `release/win-unpacked.bak` restored automatically on a
  failed integrity-gated staged swap.
- Source: revert to base 9135f1b0dd0ad4ab4587d97d8c0d5e8c39091906 (additive;
  governed defaults 0; new event/request rows inert for non-governed work — no
  data migration to undo).

## Governance note
Code + tests only. Live-gateway restart, config enabling of governed mode on any
board, all-board migration, and any remote push/PR/publish remain separately
gated on Wessam's approval via Hermes. No OS/ACL/service-identity security is
claimed.
