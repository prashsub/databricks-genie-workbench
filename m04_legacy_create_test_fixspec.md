# M04 integration reconciliation — retire legacy create-timeout display-name-match tests

## Context
M04 (Mutation/Observation gate) is COMPLETE: all 18 tasks committed on branch vc/m04 (18 commits on top of 5e60c88), 51 new unit tests pass. But integrating it makes **6 pre-existing tests fail** in `backend/tests/test_create_timeout_dedup.py`.

This is CORRECT behavior by M04, not a bug. Those 6 tests assert the RETIRED legacy create-on-timeout reconciliation that adopts a space by **display-name matching** (`_title_matches_requested`). The M04 design (module-04 plan line 68) explicitly prohibits this: *"Unexpected create response and possible orphan need operator evidence, never display-name lookup."* M04 task 13/17 fail-closed `create_genie_space` (backend/genie_creator.py:538 now raises `PermissionError("VC writes disabled: create requires durable intent and the mutation gate")`). The proper new create flow (durable create-intent admission before POST, quarantine possible orphan, audited orphan resolution — NO display-name match, NO blind replay) is implemented and tested in `backend/tests/test_vc_create.py` (M04-owned).

## Current state of test_create_timeout_dedup.py
- 4 tests PASS (pure helper unit tests that don't call create_genie_space): `test_is_timeout_error_detects_read_timed_out`, `test_is_timeout_error_false_for_non_timeout`, `test_title_matcher_exact_and_variants`, `test_title_matcher_rejects_prefix_siblings`. LEAVE THESE AS-IS (the helper functions still exist in genie_creator.py).
- 6 tests FAIL (all call `gc.create_genie_space(...)` expecting the old reconcile-by-display-name end-to-end flow): `test_timeout_reconciles_to_created_space`, `test_reconcile_matches_timestamp_renamed_variant`, `test_reconcile_ignores_preexisting_space_outside_window`, `test_reconcile_prefers_earliest_in_window`, `test_timeout_without_match_raises`, `test_uses_non_retrying_client_for_create`.

## Required fix (STRICT TDD, TEST-ONLY)
Reconcile `backend/tests/test_create_timeout_dedup.py` so it asserts the NEW fail-closed contract instead of the retired display-name-match flow. Do NOT re-enable the legacy behavior; do NOT touch genie_creator.py or any production code — the production behavior is correct as designed.

Specifically, for the 6 failing tests:
1. Replace the display-name-match end-to-end expectations with an assertion of the CURRENT contract: `create_genie_space(...)` (the legacy entrypoint) is fail-closed and raises `PermissionError` mentioning that create requires durable intent and the mutation gate, when VC writes are disabled (the default). This directly encodes M04 task 17 ("fail closed all legacy managed writes and default-off gate") and task 13 ("no display-name match, no blind replay").
2. Update the module docstring at the top of the file to reflect that the display-name-match reconciliation has been RETIRED in favor of durable create-intent + gated create (see test_vc_create.py), and that these tests now pin the fail-closed legacy entrypoint + the still-valid low-level helpers.
3. IMPORTANT — preserve the genuinely valuable SAFETY intent: the old `test_reconcile_ignores_preexisting_space_outside_window` encoded a real data-loss guard ("never adopt a pre-existing same-named space"). The NEW design satisfies this by never doing display-name adoption at all. Add/keep an assertion documenting that the legacy display-name-adoption path is no longer reachable (create is fail-closed), so a future regression that re-introduces blind display-name adoption would fail this test. If `_title_matches_requested` / reconcile helpers are still importable, it's fine to keep testing them in isolation (the 4 passing helper tests already do), but they must NOT be wired into an active create path.
4. `test_uses_non_retrying_client_for_create`: the "non-retrying client" requirement is still valid design (M04 task 18: disable hidden SDK retries). If the legacy create entrypoint is fail-closed, either (a) re-point this test at the NEW gated transport path if it's reachable in a test harness, or (b) if that belongs to test_genie_client.py's M04 coverage (which already has `test_transport_disables_hidden_sdk_retries_for_post_and_both_patches`), convert this test to assert the legacy entrypoint is disabled and note the non-retry guarantee is now covered by the M04 transport test. Prefer (b) to avoid duplicating/pre-enabling a disabled path. Do NOT re-enable a live create POST.

## TDD discipline
- These are already RED (failing) against the integrated code. For each rewritten test, first confirm it currently fails for the OLD reason (expects legacy behavior), then rewrite to assert the NEW contract and confirm GREEN. Capture the before/after.
- Do NOT weaken coverage: the file must still meaningfully pin (a) fail-closed legacy create, (b) the timeout/title helpers in isolation, (c) the no-blind-replay / no-display-name-adoption invariant.

## Owned paths (STRICT)
- ONLY `backend/tests/test_create_timeout_dedup.py`.
- Do NOT edit genie_creator.py, genie_client.py, or any production file — production behavior is correct as designed.

## Gates (run from worktree ROOT)
- `python -m pytest backend/tests/test_create_timeout_dedup.py -q` → all pass (10 or fewer if you consolidate, but keep the safety coverage).
- `python -m pytest backend/tests -q --ignore=backend/tests/integration -p no:cacheprovider` → GREEN (was 923 passed / 6 failed; must become all passed).

## Commit
Single commit, message: 'test: retire legacy display-name-match create reconciliation; assert M04 fail-closed create'. Commit immediately after green.
