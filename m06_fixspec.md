# M06 Cross-Vendor Review Fix Specification (VC/1.0)

Verdict: **BLOCK** — 2 blocking findings, both empirically demonstrated by the
independent reviewer against a scratch copy of the diff. Fix BOTH via strict TDD
(named RED test first, confirm meaningful failure, minimal fix, refactor green).
The crypto/consumption core is strong and must NOT be regressed. Manifest
compliance is PASS — keep all changes within M06 owned-paths only:
backend/services/version_control/governance/*, backend/routers/vc_approvals.py +
vc_operations.py, backend/version_control_ddl/06-operations.sql +
07-approval-evidence-volume.sql, backend/tests/{test_vc_operation_facts,
test_vc_approvals,test_vc_audit}.py.

---

## BLOCKING-1 — Break-glass suspension is permanent/unclearable; rebind silently lifts it

**Location:** `backend/services/version_control/governance/approvals.py:352-354`
(`suspended`), `:231-233` (`_unused`), `:390-398` (`break_glass`).

**Defect:** `suspended()` returns true if ANY fact exists under the reserved key
`'vc:break-glass-suspension'`, and `_unused()` hard-rejects every `authorize()`
on that binding. Nothing in M06 can remove/supersede that fact — the table is
append-only and the reserved idempotency key is digest-pinned to `scope_digest`,
so `DurableOperationFacts._append` rejects any follow-up under that key. Result:
one emergency permanently disables ALL future governed mutation on the binding,
including the adopt/reapply reconciliation that is supposed to be the EXIT path
(M06 §5 row 12 "mandatory reconciliation flag"; contracts.md §6 "requires later
reconciliation"). It also blocks a second distinct incident on the same binding.

WORSE: `suspended()` matches on binding_id + binding_revision + workspace_id, so
a rebind clears it (revision 1 suspended=True, revision 2 suspended=False). That
inverts F-REBIND / contracts.md §5 ("Rebind/tombstone invalidates old-revision
approvals") — the highest-scrutiny action launders away an unreconciled override,
while the reconciliation designed to clear it cannot.

**Invariant violated:** F-REBIND + M06 §5 row 12 (mandatory reconciliation flag
must have an authorized clear path; rebind must not lift an unreconciled override).

**Fix:** Model suspension as a STATE PROJECTION over an append-only chain, not
existence-of-any-row:
- Give break-glass facts their own kind-scoped query (`fact_kind == BREAK_GLASS`)
  with a distinct PER-INCIDENT idempotency key, e.g.
  `f'vc:break-glass:{request.identity.operation_id}'`, so multiple incidents can
  coexist.
- Compute `suspended` as "exists an unreconciled BREAK_GLASS fact with no matching
  later ACKNOWLEDGEMENT/RECOVERY fact referencing it via `approval_reference`".
- Scope the suspension query to `binding_id` ONLY (not revision) so a rebind
  cannot launder it.
- Require the reconciliation fact to carry FRESH authorization.

**RED tests to write first (both currently absent):**
1. `test_break_glass_suspension_is_cleared_only_by_authorized_reconciliation`
   (test_vc_approvals.py) — after `break_glass`, assert `authorize` raises;
   append an AUTHORIZED reconciliation fact; assert `authorize` now succeeds;
   assert an UNauthorized reconciliation attempt still raises; assert a second
   distinct incident on the same binding is admissible while the first is open.
2. `test_rebind_does_not_lift_unreconciled_break_glass_suspension`
   (test_vc_approvals.py) — after `break_glass` at revision 1, assert
   `service.suspended(replace(binding, binding_revision=2))` is still True and
   `authorize` against the new revision still raises.

---

## BLOCKING-2 — Vote event keys omit voter identity: a concurrent second vote bricks the approval

**Location:** `backend/services/version_control/governance/facts.py:572-578`
(`fact_key`) with `approvals.py:247-259` (`_record`), `:292-299` (`vote`).

**Defect:** `fact_key` hashes only {binding, operation_id, kind, sequence,
attempt_id, generation}. For a vote, `transition_sequence = len(record.votes)`
read from a snapshot. Two approvers voting against the same snapshot both compute
sequence=1, kind=approval_vote, identical binding/operation, attempt_id/generation
both None → IDENTICAL event_key (collision confirmed). The approver identity —
the entire semantic content of a vote — is not in the key. The two-human
production rule GUARANTEES two voters, and `vote()` has no CAS/lease/serialization.
On real Delta both commits land → ambiguous rows poison `lookup_request`,
`get_request`, `service.get`, `authorize`, AND `audit.get` permanently
(append-only, unrecoverable; break-glass also calls `self.get()` first and hits
the same error). In-process the single-threaded read-back masks it (suite passes),
but that is a TOCTOU window, not a lock.

**Invariant violated:** contracts.md — "Logical uniqueness enforced by serialized
protocol plus read-back and DETERMINISTIC EVENT KEYS, not unenforced Delta keys."
Keys must be unique per DISTINCT fact. F-APPROVAL-REUSE / F-CRASH-STAGES.

**Fix:** Include the vote's distinguishing identity in the key — add the voter
(`actor.subject_id` / approval_digest term) to `fact_key`'s payload for vote-kind
facts, so two distinct approvers produce two distinct keys and both votes durably
coexist. Derive vote ordering from the DURABLE fact set, not `len(record.votes)`
on a stale snapshot. Keep `lookup_request`'s existing behavior: byte-identical
retry rows dedupe (correct), distinct-content-same-key stays a hard error (the key
derivation is the bug, not the ambiguity check).

**RED tests to write first (both currently absent):**
1. `test_distinct_approvers_produce_distinct_vote_event_keys`
   (test_vc_approvals.py) — build vote facts for human-1 and human-2 at the same
   transition_sequence; assert `fact_key(a) != fact_key(b)`.
2. `test_concurrent_votes_from_snapshot_both_persist_and_approval_stays_readable`
   (test_vc_approvals.py) — pin `service.get` to a shared pre-vote snapshot,
   submit both votes, then assert two durable approval_vote rows,
   `lookup_request(...).ambiguous is False`, `service.get` succeeds, and
   `authorize` returns a grant.

**Ordering:** BLOCKING-2 (fact identity) should land first — BLOCKING-1's break-
glass reconciliation facts also flow through the same fact-key machinery, and
finding #5 (`_publish_grant` write-on-read-path) interacts with the key
derivation; re-check it after BLOCKING-2.

---

## NON-BLOCKING (address the cheap/relevant ones; note the rest as handoffs)
1. `invoke()` funnels programming bugs into 503 evidence_unavailable
   (vc_approvals.py:49-51) — narrow the final `except` to real I/O/storage types.
2. `Idempotency-Key` not enforced on POST /votes vs contracts.md §7 — enforce or
   explicitly waive in contract (defensible since durable RequestIdentity carries
   the real key).
3. Route prefix `/api/vc` vs contract `/api/version-control` — reconcile (M08
   remounts; note as handoff).
4. `DeltaFactStore.read()` scans/integrity-checks the whole workspace per call
   (delta.py:512-522) — push binding/operation predicates into SQL; one dead blob
   fails every op workspace-wide. Fail-closed is correct but blast radius too wide.
5. `_publish_grant` mutates approval status on the read path of `authorize`
   (approvals.py:344-350) — idempotent/re-entrant so non-blocking; re-check after
   BLOCKING-2.
6. DDL adds CHECK (binding_revision > 0) not in normative block — harmless
   tightening; note only.
7. `integration` marker unregistered (M08-owned) — handoff to M08.
