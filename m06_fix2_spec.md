# M06 Fix Re-Review — 1 blocking finding (verification regression)

Verdict: BLOCK, 1 medium finding. BOTH original blockers (BLOCKING-1 launderable/
permanent suspension, BLOCKING-2 vote-key collision) are CONFIRMED genuinely
resolved with sound mechanisms + real RED coverage — do NOT touch those; do not
regress them. The crypto/consumption core is intact. This fix is small and
surgical.

## FINDING 1 (blocking) — the BLOCKING-2 fix silently disarms the F-APPROVAL-REUSE assertion in an existing acceptance test

**Location:** `backend/services/version_control/governance/facts.py` `get_request`
(diff:207-209 `records` filter) interacting with
`backend/tests/test_vc_approvals.py::test_historical_approval_reuse_and_different_request_digest_are_rejected`,
parametrization `fault='consumed'`.

**Why:** The new filter excludes `APPROVAL_VOTE` from `records`, so votes no longer
occupy sequences 1..n. In the `'consumed'` branch the test appends an
`APPROVAL_CONSUMED` fact with hardcoded `transition_sequence=0` and `evidence=record`
(the 2-vote record), and never calls `authorize`, so no `APPROVAL_GRANTED` exists.
`records` collapses to `[approval_request(seq 0), consumed(seq 0)]` with DIFFERING
evidence at max sequence, so `get_request` raises `ValueError('Ambiguous approval
evidence')` inside `self.get()` at the top of `_authorize` — BEFORE reaching the
`_unused` consumption guard (`'Approval consumed; only existing-claim verification/
publication may recover'`). The test still "passes" only because it accepts
`pytest.raises((PermissionError, ValueError, OSError))`. So M06 §5 row 10 / §6
F-APPROVAL-REUSE ("a real durable consumption fact blocks reuse") is now UNVERIFIED
end-to-end, masked by a broad exception tuple — the exact failure mode that let the
original defects through.

**Not a production hole:** `publish_consumption` requires `record.status == APPROVED`
(⇒ a GRANTED fact at seq ≥ 1) on the normal path and uses `BreakGlassRequest`
evidence (excluded from `records`) on the override path, so no legitimate ledger
reaches the raise. But it lands on the "consumption core must NOT be regressed"
constraint and must be fixed so the invariant is actually tested.

**Fix (TDD):**
1. In the test's `'consumed'` branch, publish the grant BEFORE appending the
   consumption (`context.service.authorize(context.request, context.executor)`, or
   append an `APPROVAL_GRANTED` fact) so the consumption is not the max-sequence
   `ApprovalRecord` row.
2. Tighten the assertion from the 3-type tuple to the specific rejection:
   `pytest.raises(PermissionError, match='consumed')` for `fault='consumed'`.
3. Optional hardening: give `APPROVAL_CONSUMED` a DERIVED `transition_sequence`
   instead of hardcoded `0` so it cannot share a sequence with `APPROVAL_REQUEST`.

**Named RED test to add:**
`test_durable_consumption_fact_blocks_reuse_after_grant_is_published`
(test_vc_approvals.py) — approve; authorize (grant published); append the
`APPROVAL_CONSUMED` fact with the approved record's `approval_digest`; assert
`facts.get_request(uid(2)).approval.status == FactStatus.APPROVED` (get_request does
NOT raise); then assert `pytest.raises(PermissionError, match='consumed')` on
`service.authorize(context.request, context.executor)`. Must fail first (currently
raises ValueError from get_request), then pass after the fix.

## Non-blocking (optional; address cheap ones, note rest as handoffs)
1. `unauthorized` RED fixture over-determined (fails on 3 counts); add a case that
   asserts the freshness branch specifically (well-formed ACK backed by a grant
   requested BEFORE the incident).
2. `reconcile()` is a new public ApprovalService method absent from module-06 §3's
   contract list — name it as the §5-row-12 exit path (doc only; no route drift).
3. `binding_facts` drops `workspace_id` — consider scoping to full BindingRef minus
   revision (fail-closed, safe today).
4. `reconcile`'s `1 + max(...)` has no `default=` (brittle, unreachable today).
