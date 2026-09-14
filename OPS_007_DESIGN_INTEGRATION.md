# Slip #009 — Ops #007-OPS design integration statement

Ops design read IN FULL:
C:\Users\DELL\AppData\Local\hermes\profiles\ops\workspace\research\2026-09-14-windows-board-service-identity-acl-ipc-design.md
(138 lines; inspected at Hermes source commit 359190776094…).

## How this slip aligns with the Ops design
- Ops §1/§8: the board is a shared same-user SQLite file; app-level guards are
  "UX, not a trust boundary." This slip does NOT claim to fix that — it removes
  the confused-deputy control-plane hole and marks the raw-DB residual as an
  explicit UNKNOWN pending ACL (test_raw_db_write_bypass_is_unresolved_residual_ops_007).
- Ops §2: recommends a dedicated Windows virtual service account
  (NT SERVICE\HermesBoard) as the single distinct writer SID. This slip's
  should_mint_board_authority() establishes the SINGLE-OWNER ROLE in code now
  (default-home process only), so the later cutover maps that role onto the
  service account with no re-architecture — the role seam is ready.
- Ops §4: reuse the control_socket pattern; the PIPE DACL is the auth, and
  privileged verbs must be authorized per caller SID. This slip's conclusion is
  stronger for the interim: since no per-caller SID auth exists yet (all agents
  run as DELL), the privileged verbs are REMOVED from the generic socket
  entirely rather than left exposed. When the service account + ACL'd pipe with
  a SID allow-list exists (§4/§9 T3), a privileged ingress can be reintroduced
  behind that authenticated boundary.
- Ops §6: writes must fail closed when the service is unavailable, never fall
  back to a direct same-user write. This slip's non-owner path is request-only
  (no fallback to authoritative write); the board-owner path is the only executor.
- Ops §9: every security claim is bound to a future live test (icacls readout,
  direct-write denial, IPC authorization, impersonation denial, fail-closed,
  audit trail). This slip asserts NO ACL/live security; those T1–T6 tests remain
  the acceptance gate for the separately-approved cutover.

## What this slip deliberately does NOT do (per Ops staging §7 + slip scope)
- No virtual account creation, no icacls, no board relocation to a service root,
  no HERMES_KANBAN_HOME repoint, no client migration of desktop/gateway/backup,
  no live service. Those are the Ops §7 step-3 cutover, separately approved.
- The DB schema/format is unchanged (Ops §7/§10 keep rollback a permissions+path
  revert), preserved here.

## Remaining live UNKNOWNs owned by Ops #007-OPS (unchanged by this slip)
Effective DACLs, direct-write denial for DELL, ACL'd named-pipe authorization,
impersonation denial, capability-file confidentiality on Windows, and backup
side-channel handling — all remain to be proven under a separately approved live
window via Ops §9 T1–T6.
