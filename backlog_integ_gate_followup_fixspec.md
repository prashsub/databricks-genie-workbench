# Integ-gate review follow-up — restore the "never shrinks below landed baseline" invariant

VERDICT was APPROVE with one required follow-up. A/B/C are all real and verified (the A ratchet is genuinely NON-tautological: declared_count = AST parse of files on disk vs all_cases = pytest collector subprocess with -m "" — two independent oracles; deleting a pytestmark line makes them diverge -> RED). Do NOT change A/B/C's mechanism.

## The follow-up — a distinct invariant was dropped
The OLD `>= 10` (later described as the floor) was ALSO carrying a separate "monotonic ratchet" invariant: the integration suite count can only GROW, never shrink below the highest baseline that has landed. The reviewer referenced a test named like `test_integration_suite_never_shrinks_below_landed_baseline`. Replacing the magic floor with the declared==collected==recount equality check (which catches DECLARATION drift, i.e. a marker removed while the test file stays) is strictly better for that class — BUT it no longer catches the case where an entire integration TEST/FILE is DELETED (both declared and collected shrink together to a new lower equal number, so equality still holds -> stays GREEN). The old floor would have caught "someone deleted integration tests and dropped the total below the landed baseline".

## Required fix (TEST-ONLY, backend/tests/test_vc_provisioning.py):
Restore the monotonic baseline ratchet ALONGSIDE the new equality/recount checks (keep A/B/C). Add an explicit assertion that the collected integration count is >= a LANDED BASELINE constant equal to the current real count (15). This is the ratchet: it may only be raised in a deliberate commit, never lowered. So:
- assert collected_integration_cases >= 15   # LANDED_INTEGRATION_BASELINE; raise only when new integration tests land, never lower
- keep the A equality check (declared == collected == independent recount) for declaration-drift
- Make the baseline a clearly-named module constant with a comment explaining it is a deliberate ratchet.

IMPORTANT: this must NOT break the synthetic-fixture tests that invoke the gate fn against a monkeypatched tmp ROOT with a SMALL fake layout (~10-12 cases). So the baseline assertion must apply ONLY to the REAL tree (the top-level gate test), NOT inside the gate function that the synthetic-layout tests reuse against tmp ROOTs. Put the >=15 baseline assertion in the real-tree gate test only, or guard it so the synthetic-layout invocations (different ROOT) are exempt. Verify all synthetic-fixture tests still pass.

## TDD:
- Add a RED test proving deletion of an integration test/file (dropping the real total from 15 to <15) now goes RED (it currently stays GREEN because declared==collected both shrink). Confirm RED-first on current vc/backlog-integ-gate HEAD, GREEN after adding the baseline ratchet.
- Re-run full test_vc_provisioning.py + full backend offline suite; real declared==collected==recount==15 still holds; synthetic-fixture tests intact.

## Owned-paths: ONLY backend/tests/test_vc_provisioning.py (test-only). No production code, no other test files.

Report: commit SHA, the RED->GREEN evidence for the restored baseline ratchet, confirmation synthetic-fixture tests still pass, final counts (provisioning suite + full offline + integration collect =15).
