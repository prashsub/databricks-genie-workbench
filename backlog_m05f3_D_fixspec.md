# M05-F3 review follow-up — Finding D (test-quality regression)

VERDICT was BLOCK on ONE finding only. The production fix (fail-closed when approved_targets absent) is CORRECT and COMPLETE (reviewer brute-forced 2996 combos, 0 bypasses; F1/F2 not regressed; new tests genuinely RED-first). Do NOT change the production fix in drift/service.py.

## Finding D — fixture update weakened a pre-existing assertion
When making F3 fail closed, the fix updated existing happy-path fixtures in test_vc_drift.py / test_vc_reconcile.py so they now "supply valid rendered approval evidence" (i.e. set approved_targets to a non-None value) so those tests still pass. That is the correct direction, BUT in doing so at least one pre-existing assertion was weakened/rendered less meaningful (an assertion that previously exercised a specific behavior now passes trivially, or a broad exception/branch is accepted).

The reviewer confirmed the CORRECTIVE fix is fully compatible: with it applied the full suite is 1250 passed / 15 deselected AND the 3 F3 guards remain RED-first (reverting the classify guard to `is not None` still fails 7 cases). So the corrective fix does not weaken F3 coverage.

## Required fix (TEST-ONLY, M05 owned test files):
Restore the pre-existing assertion strength that the fixture update weakened. Concretely:
- Find the happy-path test(s) in test_vc_drift.py / test_vc_reconcile.py whose fixture was changed to add approved_targets/rendered-approval evidence as part of the F3 fix.
- Ensure those tests still assert their ORIGINAL intended behavior meaningfully (not merely "no exception" / not merely passing because approved_targets is now present). If an assertion was broadened (e.g. accepting a wider set of DriftStates or a broad exception tuple), tighten it back to the specific expected value.
- Add/keep an assertion proving the happy path genuinely reaches and passes the target-rendered check with valid evidence (positive path), distinct from the F3 fail-closed negative path.

## TDD:
- Identify the weakened assertion (compare the diff of the fixture/assertion change at 22411f0..vc/backlog-m05f3 in test_vc_drift.py + test_vc_reconcile.py).
- Restore/tighten it; confirm the test still passes with the real fix AND would fail if the happy-path behavior regressed (mutation-check: the tightened assertion should be non-vacuous).
- Re-run: M05 suites (test_vc_drift, test_vc_reconcile, test_vc_dual_authority) + confirm the 3 F3 guards still RED-first when the classify guard is reverted + full backend offline suite green (1250).

## Owned-paths: ONLY test_vc_drift.py + test_vc_reconcile.py (test-only). Do NOT touch drift/service.py (production fix is approved). No other module.

Report: commit SHA, which assertion was weakened + how restored, mutation evidence it's now non-vacuous, final counts.
