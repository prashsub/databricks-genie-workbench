# M03 fix3 — close BLOCK-12 WEAK-COVERAGE (test-only)

## Context
The M03 blocker-fix (vc/m03 @ b19afde) resolved 11/12 blockers with genuine mutation-confirmed RED coverage. Cross-vendor re-review found ONE remaining gap, and it is **test-only** — the production fix is complete and correct.

## The finding (BLOCK-12, assertion 6 — attempt-fence CAS predicate)
Fixspec §(d) assertion 6 requires the checkpoint CAS predicate `r.attempt_id == claim.attempt_id` (in `backend/services/version_control/coordination/service.py`, the checkpoint fence) to be PROVEN load-bearing by a RED test.

The shipped test `test_checkpoint_replay_skip_and_attempt_fencing` (assertion 6, in `backend/tests/test_vc_coordination.py`) is **tautological w.r.t. this fix**:
- It swaps in a foreign-`attempt_id` row **before** calling `checkpoint`.
- So the rejection comes from PRE-EXISTING code — `service.py:206` in `_owned` via `assert_owner` — and never reaches the new CAS predicate.
- Proof the test proves nothing: reverting the fence to `lambda r: r.mutation_stage == row.mutation_stage` still yields **107 passed** (zero tests fail).

The reviewer independently confirmed the CODE is correct via a TOCTOU probe:
- fence present → dead/stolen claim rejected, stage stays `CONFIG_IN_FLIGHT` ✅
- fence removed → checkpoint advanced a **stolen attempt** to `CONFIG_OBSERVED` ❌

## Required fix (STRICT TDD, test-only)
Amend `test_checkpoint_replay_skip_and_attempt_fencing` (or add a focused sibling test) so the takeover/attempt-substitution happens **inside the CAS window** — i.e. after `_owned`/`assert_owner` has read ownership but before/at the moment the checkpoint CAS predicate evaluates — modeling a genuine TOCTOU: a takeover that lands after the ownership read, before the predicate. This is the same technique the reviewer used in its TOCTOU probe.

Acceptance:
1. **RED FIRST**: with the production fence reverted to `lambda r: r.mutation_stage == row.mutation_stage`, the amended assertion 6 MUST fail (proving it exercises the `r.attempt_id == claim.attempt_id` predicate, not pre-existing `_owned` logic). Capture that RED evidence.
2. **GREEN**: with the real fence in place, the amended test passes.
3. Do NOT touch production code — `service.py` is already correct. Only the test file changes.
4. Keep all existing assertions in that test intact (stage-scoping RED already works); only strengthen the attempt-fence half.
5. Full coordination + recovery suites stay green (107 passed baseline; this stays 107 or +N if you add a sibling test).

## Owned paths (STRICT)
- ONLY `backend/tests/test_vc_coordination.py` (test-only fix).
- Do NOT edit `service.py`, DDL, or any other file.

## Commit
Single commit, message naming BLOCK-12 attempt-fence coverage. Incremental commit after the GREEN cycle.
