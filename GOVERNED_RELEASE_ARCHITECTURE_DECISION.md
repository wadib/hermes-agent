# Slip #006 — Architecture Decision Memo: governed release authority is not
# achievable in-process (BLOCKED-ARCHITECTURE)

Status: BLOCKED-ARCHITECTURE (secure release portion). Escalation to Hermes.
Author: Relay. Worktree: .worktrees/slip006-server-owned-release, branch
relay/slip006-server-owned-release, off 65542e1d046f09a824ee903345a32f3a2a1add87.
NO remote / restart / live board / config / build / deploy action taken.

## Bottom line
The two Slip #005 HIGHs share ONE root cause, and it is architectural, not a
code bug I can patch in-process without weakening the approved invariant:

  The Kanban board is a plain SQLite file under HERMES_HOME, writable by the
  same OS user that every profile agent (and every delegated child with terminal
  access) runs as. There is NO privilege/service/OS boundary between an ordinary
  agent and the authoritative board state.

Given that, NO application-level release gate can be made unforgeable: an agent
that can run `python -c "import sqlite3; ..."` (or `sqlite3 kanban.db`) simply
writes `execution_authorized=1` directly, bypassing every Python guard, identity
check, capability token, or config policy I could add. Per the slip's explicit
instruction ("Do NOT claim security from an in-process token, HERMES_HOME,
environment secret, hidden CLI flag, or file readable by the same agent"), I am
stopping the secure release portion and escalating the design decision.

## Exact bypasses (both reproduced on a DISPOSABLE temp board — never the live board)
Reproduction: probe run under the checkout venv against a throwaway HERMES_HOME.

BYPASS 1 — raw DB write, no app logic at all:
  Created a governed task via the real app -> {status: todo, execution_authorized: 0}.
  Board DB path resolved to <tmp>/.hermes/kanban.db; os.access(path, os.W_OK) == True.
  Ran:  sqlite3.connect(path).execute(
          "UPDATE tasks SET execution_authorized=1, status='ready' WHERE id=?")
  Re-read via the app -> {status: ready, execution_authorized: 1}.  <-- BYPASS
  No release, no caller_identity, no CLI, no dashboard — the guard code never runs.

BYPASS 2 — profile-home identity spoof (Sentinel's finding, re-reproduced):
  Set HERMES_HOME to <...>/profiles/hermes; get_active_profile_name() -> "hermes".
  release_task(..., actor="anything", caller_identity="hermes") -> ok=True,
  execution_authorized becomes 1.  <-- BYPASS
  The identity that my Slip #004 gate trusts is chosen by the caller's env.

Source confirmation (read, not inferred):
- hermes_cli/profiles.py:1376-1390 — get_active_profile_name() derives identity
  SOLELY from HERMES_HOME path shape. Caller-settable.
- hermes_cli/kanban_db_connect.py:667-733 — connect(db_path=...) opens ANY path;
  board is <HERMES_HOME>/kanban/<board>/kanban.db (kanban_db.py:605-612).
  Delegated-child processes get a read-only URI (:676-684), but an ORDINARY
  profile agent (not a delegate_task child) has full read/write and can also
  connect a raw sqlite3 handle outside the app entirely.
- No service process owns the DB; writes are in-process to a user-writable file.

## Threat model / permissions of an ordinary agent with terminal access
An ordinary profile agent can: (a) run arbitrary Python/sqlite3 against the board
file; (b) set HERMES_HOME/-p to any profile path; (c) read any file the user can
read. Therefore any secret/token/flag/identity that lives in-process or in a
same-user-readable file is available to the attacker we are defending against.
The delegated-child read-only guard is real but only covers `delegate_task`
descendants, not a peer profile agent or a raw shell.

## Why this blocks BOTH findings, not just release
The internal-acceptance liveness fix (Finding 2) is NOT independently completable
either, because a durable "Hermes accepts internal governed work" route needs the
SAME trusted-acceptance authority that Finding 1 shows is forgeable. Building an
internal acceptance queue now would let any spoofed "hermes" accept its own work —
disguising the unresolved authority model, which the slip forbids. The liveness
deadlock and the authority bypass must be solved by the same boundary decision.
(The liveness symptom itself — internal no-session governed work forced to
delivery_pending with an outbox that has no platform/conversation recipient, so
it can never reach awaiting_acceptance — is real and confirmed at
kanban_db.py:3521-3556 + the awaiting_acceptance route requirement; it is a
consequence of routing internal work through the conversation-delivery machinery.)

## Smallest real isolation options (for Hermes to choose)
Ranked smallest-blast-radius first. All require a decision above my authority.

Option A — Service-owned board behind a local authorization boundary.
  A single trusted board service (the gateway/Desktop `serve` process) owns the
  ONLY read-write handle to kanban.db; the file is made non-writable to other
  contexts (OS ACL / separate service account / dir perms). Ordinary agents get
  governed-release/accept ONLY by calling the service over its existing local
  ingress (the gateway/TUI JSON-RPC / dashboard API), where identity is the
  server-resolved session, not caller env. Governed release/accept become
  service-only RPCs; raw DB writes are removed by filesystem permission.
  - Pros: real boundary; reuses existing trusted ingress; matches approved
    intent 6/7. Cons: needs an OS-level ACL story on a single-user Windows box
    (may require a separate service account to be meaningful), and a migration
    for how CLI `hermes kanban release` reaches the service. Largest but only
    truly-unforgeable option under this threat model.

Option B — Remove raw board/tool access from ordinary agents.
  Restrict which contexts may open the board read-write at all (extend the
  delegated-child read-only rule to all non-service contexts) AND gate the
  release/accept RPC behind the server session identity. Still relies on the OS
  preventing a raw sqlite3 open by the same user — so only as strong as file
  permissions; without an ACL/separate account it is not unforgeable.

Option C — Accept the residual (explicit Wessam decision).
  Treat profile identity as a non-adversarial local convention and have Wessam
  explicitly accept that any same-user actor with board access may impersonate
  hermes. Then the Slip #004 gate stands as a non-security operational control
  (audit + accident-prevention), NOT a security boundary, and the release plan
  says so plainly. Zero new architecture; honest downgrade of the claim.

## Migration impact
- Option A/B change how `hermes kanban release`/accept reach the DB (direct ->
  service RPC); needs the board service always-running and a fallback when it is
  not. Filesystem-permission changes affect every board reader/writer and the
  updater/backup paths that touch kanban files. Non-trivial; must be scoped as
  its own slip with its own restart/deploy window.
- Option C is documentation-only.

## Safe interim mitigation (no architecture, no false security)
Until a boundary is chosen, keep the Slip #004 state guards (they DO stop the
reproduced dashboard/complete_task bypass for non-adversarial use) but label the
release control in GOVERNED_MODE_RELEASE_PLAN.md as "operational/audit only, NOT
a security boundary — same-user impersonation is possible; see this memo." Do NOT
enable governed mode on any board as a security feature until Option A/B ships.
Do NOT build the internal acceptance route yet (it would inherit the forgeable
authority). This is fail-closed-honest: no task is falsely marked Done, and no
security claim is made that the runtime cannot keep.

## What I did NOT do
No secure release implemented (would be theatre under this threat model). No
internal acceptance route implemented (entangled with the authority decision). No
remote/restart/board/config mutation. Prior worktrees preserved. The reviewed
branch carries this memo only; the reproduction ran against disposable temp dirs.

## Requested decision from Hermes
Pick A, B, or C (or direct a different boundary). If A/B: authorize a scoped
follow-up slip for the service/ACL work with its own restart window. If C:
Wessam's explicit residual-risk acceptance, after which I downgrade the release
plan's security claim and (separately) implement the internal acceptance route
under the accepted trust model.
