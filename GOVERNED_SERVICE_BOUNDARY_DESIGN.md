# Slip #007 — Service-owned board boundary: design + threat-boundary memo

Worktree: .worktrees/slip007-service-owned-board
Branch: relay/slip007-service-owned-board  Base: 5d28f31ef712de4f4c576e97d117e3938731e5f8
Status target: BLOCKED vertical-slice handoff (honest) — full unforgeability
depends on OPS #007-OPS (separate Windows service identity + ACL), UNKNOWN until
separate live approval. NO live account/service/ACL/DB-migration/restart/remote.

## Threat-boundary inventory (source-of-truth read, not inferred)
Raw board writers / authority mutators (who can set execution_authorized / land
governed done today):
- hermes_cli/kanban_db.py:release_task (4170) — sets execution_authorized=1.
- hermes_cli/kanban_db.py:accept_delivery_from_gateway (2616) / _from_desktop
  (2689) — governed done via acceptance.
- hermes_cli/kanban_db.py:complete_task (3471) — governed -> delivery_pending.
- ANY process: sqlite3.connect(<board>/kanban.db) raw UPDATE (Slip #006 proof).
Board path: <HERMES_HOME>/kanban/<board>/kanban.db (kanban_db.py:605-612),
opened by connect() (kanban_db_connect.py:667) — plain file, os.W_OK=True for
the single user. Delegated CHILDREN get a read-only URI (:676-684); a peer
profile agent or raw shell has full R/W.

Permissions of an ordinary agent with terminal access: run arbitrary
python/sqlite3; set HERMES_HOME/-p to any profile path; read any same-user file.
=> Any in-process token or same-user-readable capability file is available to the
attacker. THIS IS THE HARD GATE: full protection needs the DB (and the service
capability) writable/readable ONLY by a separate Windows service identity via
ACL. That is OPS #007-OPS and is not delivered here.

## Reused seam (not new IPC)
gateway/control_socket.py::GatewayControlServer already is the gateway-owned
local ingress: local-only socket (POSIX 0o600 umask+chmod) / Windows named pipe;
"filesystem/pipe ACLs are the auth boundary" (its own docstring). It accepts
extra verb_handlers. We add governed-release / governed-accept verbs answered
IN the trusted gateway/serve process, which holds the capability in memory. The
CLI/agent becomes a CLIENT that never holds the capability.

## Design (smallest credible slice)
1. hermes_cli/kanban_authority.py (new): the capability is minted by the trusted
   service at boot (secrets.token_hex) and written to a capability file with
   restrictive perms (0o600 now; ACL to the service identity under OPS #007).
   verify_capability() constant-time compares. is_service_context() reports
   whether THIS process holds the in-process capability (set only by the trusted
   serve/gateway boot), so a bare CLI/agent cannot self-assert it.
2. release_task(capability=...) — governed release is authorized ONLY by a valid
   service capability, NOT by caller_identity/HERMES_HOME/actor. caller_identity
   is demoted to audit provenance. No/invalid capability => fail closed.
3. CLI `hermes kanban release` becomes a control-socket client: it asks the
   gateway (which holds the capability) to perform the release. No reachable
   gateway => refused (fail closed). The CLI never reads the capability.
4. Internal acceptance route: internal (no-session) governed work gets a durable
   internal acceptance record the named owner (hermes) consumes via a trusted
   accept path — so it no longer deadlocks at delivery_pending with no recipient
   (Slip #005 HIGH #2). Missing route => reject before release/complete, not
   silent strand.

## What is REAL vs SIMULATED/UNKNOWN (no security theatre)
REAL, tested now (in-process, real imports):
- release refused without a valid capability; succeeds once with it; idempotent;
  concurrent double-release => one event.
- caller_identity/HERMES_HOME spoof CANNOT authorize (capability, not identity).
- service-unavailable => CLI release fails closed.
- internal governed create->release->complete->internal-acceptance-pending->
  named-owner accept->done E2E; non-governed legacy unchanged.
SIMULATED / UNKNOWN until OPS #007-OPS (separate Windows identity + ACL):
- That an ordinary same-user agent cannot READ the capability file or RAW-WRITE
  the DB. On today's single-user/no-ACL box BOTH bypasses remain (Slip #006
  strict-xfails stay xfail — NOT presented as resolved). The capability check is
  only a real security boundary once the DB + capability file are ACL-restricted
  to the service account. This memo does NOT claim the in-process token secures a
  same-user box.

## Remaining isolation work (exact, for OPS #007-OPS)
- Create a dedicated low-priv Windows service account; run the board service
  (gateway/serve) as it. ACL kanban.db + capability file to that account
  (deny write/read to the interactive user). Migration: move existing board dir
  under the service ACL; provide a client-only path for CLI/agents. Rollback:
  restore prior ACLs + file location; the RPC client falls back to fail-closed.
- IPC hardening: peer-credential check on the socket where the OS provides it.
