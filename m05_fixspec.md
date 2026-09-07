# M05 Drift/Reconciliation — cross-review BLOCK fixspec (F1, F2 blocking; F3, F4 non-blocking)

Source: cross-vendor review (claude_code conv 3773493063976022), 12 adversarial probes + 17-mutant campaign, 14/17 killed. Module is strong; two genuine FAIL-OPEN defects must be fixed. Reviewer prototyped F2's fix and validated F1's direction.

Work in the existing M05 worktree /home/sandbox-agent/workspace/worktrees/m05 (branch vc/m05, HEAD 851f677). STRICT TDD: write the named RED test FIRST, confirm it fails for the right reason, then fix. Commit per blocker (Astra rate-limits). Owned paths: backend/services/version_control/drift/, backend/routers/vc_reconcile.py, backend/jobs/vc_reconcile.py, backend/tests/test_vc_drift.py, test_vc_reconcile.py, test_vc_dual_authority.py ONLY.

## F1 (BLOCKING, fail-open) — scan() publishes observer-supplied CLEAN badge unverified when ledger is None
- Location: backend/services/version_control/drift/service.py:410 (the scan guard omits ledger) and :636 (`if self.ledger is None ... return status` passes the observer label through).
- Defect: with ledger=None, an observer returning drift=CLEAN while heads are observed=v2/approved=v1/deployed=v1 publishes CLEAN with allowed_actions=('compare','adopt','reapply','acknowledge') and stale=False. ledger=None is a silent pass-through, NOT fail-closed. Unlike the UNREACHABLE/CONFLICTED early returns (already blocking, so pass-through is safe), CLEAN is not blocking -> fail-open. Violates invariant 3 (capture never clears drift / never trust observer label) and FM-DRIFT.
- Required fix: add `or self.ledger is None` to the line-410 guard, and make `_observed_status` return UNKNOWN + allowed_actions=() + a reason (e.g. "Ledger unavailable; cannot verify observed head") instead of passing the observer label through when ledger is None.
- Required RED test: `test_scan_without_ledger_cannot_publish_observer_clean_badge` in test_vc_reconcile.py — with ledger=None and an observer returning CLEAN over mismatched heads, assert status.drift != CLEAN (UNKNOWN), allowed_actions==(), stale/ reason set. Verified RED against current code.
- IMPORTANT: this fix will legitimately break the ~24 existing scan tests that compose WITHOUT a ledger. UPDATE them to inject a ledger (the reviewer notes this also removes an existing tautology where 5 of 6 scan_setup call sites omit ledger and re-read the fixture's own label). After the fix, those scan tests must inject ledger=ledger so they genuinely exercise reclassification.

## F2 (BLOCKING, fail-open) — reconcile() admits adopt/reapply while a DAB bundle holds dual authority
- Location: backend/services/version_control/drift/service.py:513-575 (`_reconcile` never consults self.bundles).
- Defect: with a sanctioned bundle managing resources.genie_spaces.<space> for the exact target space, adopt SUCCEEDS — bundles.for_binding is never called and approvals.request fires. Dual authority is detected ONLY on the async scan() path; a direct reconcile route call (or M09 UI) bypasses it. classify(conflicted=...) has NO production caller (grep: zero), so a fresh binding not yet scanned has no CONFLICTED status to block on. Violates FM-DUAL-AUTHORITY ("detected within one reconcile cycle") and would file an approval request driving a PATCH into DAB-managed content.
- Required fix: add a `_require_single_authority(binding)` check in `_reconcile` AFTER `_require_coordination`, using the SAME join predicate as `_bundle_status` (workspace + space + manages_content + resources.genie_spaces. prefix), failing closed: RuntimeError when inventory is absent/incomplete, ReconcileError(409) on a detected conflict. (The reviewer prototyped this fix and confirmed it works with no other test regressing.)
- Required RED test: `test_reconcile_is_blocked_while_a_bundle_holds_dual_authority` in test_vc_reconcile.py — sanctioned bundle for the target space present, adopt/reapply must raise ReconcileError(409) (or RuntimeError if inventory incomplete), approvals.request never called. Verified RED (DID NOT RAISE) and GREEN after the prototype fix.

## F3 (NON-BLOCKING) — approved_targets=None silently skips the target-rendered approval check
- Location: service.py:677. With target approvals unwired, three identical source-environment hashes yield CLEAN with no target-rendered evidence demanded. The check itself is rigorous; risk is compositional (M08 owns wiring).
- OPTIONAL fix (do if cheap without scope creep): add a `require_rendered_targets` flag (default fail-closed) OR document that M08 must wire approved_targets and add the composition assertion to M08's test_vc_composition.py (M08-owned — do NOT edit here; instead leave a note). Prefer: add the flag defaulting to the safe behavior within M05's owned code if it doesn't break the port contract. If it risks scope creep, RECORD as backlog and skip.

## F4 (NON-BLOCKING, cheap) — scan_enabled default-off not pinned by a test
- Add `assert DriftService(canonicalizer=...).scan_enabled is False` alongside the existing reconcile_enabled/dispatch_enabled default-off assertions (mutant M14 survived). Do this — it's a one-line pin.

## TDD + gates
- STRICT TDD: F1 and F2 RED tests FIRST (both verified RED by the reviewer), then fix. Update the ~24 ledger-less scan tests as part of F1.
- Run from worktree ROOT:
  - `python -m pytest backend/tests/test_vc_drift.py backend/tests/test_vc_reconcile.py backend/tests/test_vc_dual_authority.py -q` green
  - full offline `python -m pytest backend/tests -q --ignore=backend/tests/integration -p no:cacheprovider` green (was 1061; must stay green after F1's test updates)
- Confirm write switches default-off (test_vc_composition.py) and integration collection unchanged (14).

## Commits
One commit for F1 (fix + scan-test ledger updates), one for F2 (+its RED test), one small commit for F4. Incremental commits mandatory.
