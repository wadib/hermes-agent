# Slip #002 — Governed Mode design note (Council/Command Kanban)

Worktree: C:\Users\DELL\AppData\Local\hermes\hermes-agent\.worktrees\slip002-governed-mode
Branch: relay/slip002-governed-mode  Base: 359190776094ec9f2e5ec16e18e9dc8d63c7864d
Scope: code + tests only. No restart/deploy/board mutation/config rollout.

## Policy seam (approved decision 6: distinct, testable representation — NOT repurposing execution_authorized)
Add a new per-task column `governed INTEGER NOT NULL DEFAULT 0` (kanban_db.py SCHEMA + _LATER_TASK_COLUMNS migration in kanban_db_connect.py).
- Task dataclass: `governed: bool = False`; from_row reads it; create_task takes `governed: bool = False` and INSERTs it.
- Opt-in: board metadata key `governed: true` (read_board_metadata) makes create_task default governed=True for that board when the caller does not pass an explicit value; per-task `governed=` always wins. Legacy boards (no key) → governed=False → all existing behavior byte-for-byte unchanged.
- Helper `_task_is_governed(conn, task_id) -> bool` (single source of truth) used by every chokepoint.
- execution_authorized stays the EXECUTION grant. Governed tasks are CREATED execution_authorized=0 (fail-closed); non-governed keep default 1. New explicit `release_task(conn, task_id, *, actor, reason)` sets execution_authorized=1 + `released` audit event; refuses non-governed (no-op) and already-released.

## Chokepoint fixes (each fail-closed; each guarded by governed flag so legacy is untouched)

### Class 1 — auto-promotion before explicit release
- recompute_ready() (kanban_db.py:2745): add `AND (governed=0 OR execution_authorized=1)` to the todo/blocked selection so a governed, unreleased task is NEVER promoted to ready — parent-done alone cannot grant readiness. claim_task already fail-closes on execution_authorized (:2882), but recompute_ready must not even queue it (else ready→reject loop).
- promote_task() (kanban_db.py:3948): a governed task requires release; operator promote of a governed unreleased task refuses with a governed-mode reason (release is the knob).

### Class 2 — same-profile review auto-redispatch
- request_review() (kanban_db.py:3766): when governed, require a reviewer profile distinct from implementer; refuse (with_reason) when reviewer is None/blank or equals implementer. Non-governed keeps _prior_reviewer default behavior.
- Held-not-respawned: governed review tasks are excluded from automatic review-lane dispatch. In _dispatch_once_locked (kanban_db_dispatch.py:1793) the review_rows selection excludes governed tasks (they need explicit distinct-reviewer assignment + release). A crashed/reclaimed governed review resets execution_authorized=0 so it cannot auto-claim; it waits for explicit reviewer assignment.

### Class 3 — unauthorized auto-decompose / LLM reassignment
- list_triage_ids() (kanban_decompose.py:335) / auto_decompose_tick (kanban_watchers_dispatcher.py:237): skip governed triage tasks — governed work is never auto-decomposed or LLM-reassigned. Enforced at the DB list boundary (list_tasks filter) so both the gateway tick and any direct decompose path honor it. decompose_task() also refuses a governed root (defense at the mutation, not only the lister).

### Class 4 — done-before-acceptance / durable origin binding + named acceptance owner
- Governed + conversation-originated (session_id present) ⇒ delivery_required is enforced True at create (durable origin binding via existing inherit_creator_origin + subscription). complete_task() already routes delivery_required → delivery_pending (:3326), so executor completion reaches pending only, never Done.
- Named acceptance owner: add `acceptance_owner TEXT` column. Default 'hermes' for internal governed work; caller may set the sensitive-work owner (e.g. 'wessam'). Acceptance paths (accept_delivery_from_gateway/desktop) additionally require the accepting identity to match the named acceptance_owner for governed tasks. Non-governed unchanged.

## Tests (TDD — RED first, then GREEN), all via scripts/run_tests.sh
tests/hermes_cli/test_kanban_governed_mode.py (new):
1. test_governed_task_not_promoted_or_claimable_until_release — parents done; recompute_ready + dispatch tick; assert stays todo/unclaimable until release_task, then ready.
2. test_governed_triage_not_auto_decomposed_promoted_or_spawned — governed triage; auto_decompose_tick + dispatch dry-run; assert no decompose/reassign/promote/spawn.
3. test_governed_review_requires_distinct_reviewer_and_holds_on_crash — request_review same profile refused; distinct ok; crashed review stays held, no same-profile auto-spawn.
4. test_governed_conversation_task_cannot_complete_direct_to_done — governed+session_id; complete_task → delivery_pending; only named acceptance owner finalizes.
5. test_nongoverned_legacy_behavior_unchanged — non-governed task promotes/claims/decomposes/completes exactly as today (back-compat contract).

## Back-compat proof
governed defaults 0 everywhere (column default, dataclass, create param). Every guard is `if governed and ...`; a legacy board/task takes the identical code path it takes today. Migration adds the column with DEFAULT 0 / acceptance_owner NULL — historical rows read exactly as before.
