# M03 (Coordination / Admission) — BLOCKING fix specification

**Verdict on `.polly/m03.diff`: BLOCK.** 12 blocking findings. This document is
self-contained: it re-derives every finding from the diff and cites the governing
VC/1.0 clause, so an implementer needs nothing but this file, the diff, and the
repository.

## 0. How to read and use this document

**Authority (in precedence order).** Where these disagree, the earlier wins.

1. `docs/design/version_control/implementation_plan/contracts.md` — VC/1.0.
   Cited below as **C§n** (e.g. `C§5.7` = section 5, numbered rule 7).
2. `docs/design/version_control/implementation_plan/module-03-coordination-admission.md`
   — cited as **M03§n** (ownership, ordered task table, acceptance criteria).
3. `docs/design/version_control/implementation_plan/testing-strategy.md` — cited
   as **TS** (failure matrix rows `F-*`, milestone rows `FM-*`, fake conventions).

**Files under change.** All are M03-owned per M03§2, so no cross-module
negotiation is required for any fix below.

| Path | Role |
|---|---|
| `backend/services/version_control/coordination/service.py` | 476 lines; all logic fixes land here |
| `backend/services/version_control/coordination/row.py` | 40 lines; row DTO |
| `backend/tests/test_vc_coordination.py` | 439 lines; unit/contract suite |
| `backend/tests/test_vc_recovery.py` | 140 lines; recovery suite |
| `backend/tests/integration/test_vc_delta_cas.py` | 511 lines; real-Delta gate |
| `backend/version_control_ddl/05-coordination.sql` | 39 lines; DDL (no change needed) |

**Diff line ↔ file line.** `service.py` is added at diff line 86
(`@@ -0,0 +1,476 @@`), so **`service.py:N` == diff line `N + 86`**. Every finding
below gives both. Read-only inputs you must not edit:
`backend/services/version_control/contracts.py` (M02-owned, 1006 lines) and
`backend/tests/vc_fakes/stores.py` / `fixtures.py` (M02-owned fakes).

**TDD discipline (TS "Red → green → refactor", mandatory).**

- For each BLOCK-N: write the named test in section 3(d) **first**, run it, and
  record the command plus the observed failure reason. An `ImportError`/
  `AttributeError` is **not** a sufficient red once scaffolding exists (TS step
  1) — every red below must fail on a *behavioral* assertion. Each (d) states
  the exact red symptom to expect.
- Then make the smallest change in (c). Run the focused test, then the owning
  module suite, then both M03 unit files together:
  ```
  . .venv/bin/activate && python -m pytest backend/tests/test_vc_coordination.py -q -k <name>
  . .venv/bin/activate && python -m pytest backend/tests/test_vc_coordination.py backend/tests/test_vc_recovery.py -q
  ```
- Do **not** batch fixes. One red, one green, one commit per BLOCK-N.
- Several existing tests in the diff *encode* the bugs and will go red when you
  fix them. That is expected and correct; section 5 lists each one and the exact
  amendment. Amending them is in scope (M03 owns both test files, M03§2) but
  each amendment must be justified in the handoff record against the clause in
  section 3(b) — never loosen an assertion to get green.
- `TDD.md` in the coordination package is an execution record. Append entries
  15–26 for the work below; do not rewrite entries 1–14.

**Vocabulary used throughout.**

- **Definite rejection** — the service itself evaluated a predicate over durable
  evidence it had already read, and the answer was "no". No Genie call was made
  and none is possible from this attempt. The attempt's outcome is *known*.
- **Ambiguous outcome** — an `_io` call raised, a CAS read-back did not prove
  exact ownership, or a publication could not be verified. The outcome is
  *unknown*. These must keep the row unresolved. Never conflate the two: the
  whole of BLOCK-1 and BLOCK-6 is that the diff conflates them in one direction
  and BLOCK-5 conflates them in the other.
- **Pre-send** — durable state proves no Genie mutation can have been issued for
  this attempt: `row.mutation_stage in {None, PatchStage.CONFIG_PENDING}` **and**
  `row.checkpoint` carries no `resume_classification` key. Note that
  `CoordinationService` itself never sends (`service.py:1`, "Never sends a Genie
  mutation"), so "pre-send" is always a statement about what **M04** could have
  done under a claim this service issued.
- **Possible-send** — anything else: `mutation_stage` at or past
  `CONFIG_IN_FLIGHT`, or a `resume_classification` present in `row.checkpoint`.

## 1. What the diff got right (do not regress these while fixing)

Named explicitly so the fixes below are not "simplified" into removing them:

- `_cas` (`service.py:89`, diff 175) re-reads through `_row` and rejects a CAS
  whose read-back does not prove exact holder/attempt/generation/row_version and
  every requested change. This is C§5.1 "Read back exact holder/attempt/
  generation, not just SQL success" and C§5.4 "Confirm acquisition via
  read-back". Keep it. `test_two_concurrent_reservations_have_exactly_one_winner`
  proves a lying adapter is caught (diff 1227–1233).
- `_io` (`service.py:56`, diff 142) maps every unexpected exception to
  `AuthorityUnavailable`, so no write authority leaks from a failure. C§5 preamble
  and M03§5 task 15.
- `_fence`/`_owned` fence on `(binding_revision, attempt_id, generation)` and
  never on principal identity. M03§3 "`FenceToken` … never just SP identity";
  TS `F-IDEMPOTENCY` "same SP different attempt".
- `_release` (`service.py:275`, diff 361) never erases `heads` or
  `observed_sequence`. C§5.8, TS `F-OPENS` "No head regression".
- `admit` performs exactly one CAS binding operation/key/digest, approval
  id/digest, preimage id/digest and base together (`service.py:237`, diff 323).
  C§5.4.
- `_validate_samples` refuses automatic recovery while
  `verified_request_lifetime` is unset, and production defaults it to `None`
  (`service.py:83`, diff 139). C§5.9 "Until the bound is verified, no
  auto-clear"; M03§8.
- The integration gate treats missing configuration as `fail`, not `skip`, and
  only accepts a matched `DELTA_CONCURRENT_*` error as a CAS loss (diff 742).
  TS "Never describe skipped integration tests as passed".

## 2. Ordering constraints (read before starting)

Implement in this order. The arrows are hard dependencies — taking a later fix
first will either produce a test that passes for the wrong reason or force a
rewrite.

```
BLOCK-7  (fact identity: _fact/event_key/rejected-attempt attribution)
   │  every other fix appends or reads a durable fact; fixing identity last
   │  means re-writing each new assertion
   ├─> BLOCK-1  (definite pre-send rejection releases instead of stranding)
   │      └─> BLOCK-2  (expired OBSERVING lease is reclaimed, never quarantined)
   │      └─> BLOCK-8  (terminal non-success receipt is a conflict, not a receipt)
   │             └─> BLOCK-5  (recovery closes the operation; no second admission)
   │                    └─> BLOCK-11 (recovery derives classification from state)
   ├─> BLOCK-10 (approval_consumption_published / termination_evidence truthful)
   │      └─> BLOCK-5, BLOCK-6 both read these columns
   ├─> BLOCK-6  (finish releases only on proven pre-send failure)
   │      └─> BLOCK-12 (stage machine scoped to the admitted operation)
   └─> BLOCK-3  (advance_heads requires a verified AdmissionClaim)
          └─> BLOCK-9  (approved/deployed head must be committed to this binding)
                 └─> BLOCK-4  (approved/deployed monotonic along lineage)
```

Rationale for the four non-obvious edges:

- **BLOCK-7 → everything.** Every fix below appends a fact and asserts on it. If
  `event_key` still collides and the actor is still the winner's, each new
  assertion has to be rewritten once BLOCK-7 lands, and worse, BLOCK-1's release
  path would append a second fact at the same key as the existing conflict fact
  and permanently poison the idempotency key it was meant to free.
- **BLOCK-1 → BLOCK-2.** Before BLOCK-1, a `RESERVED` row is routinely stranded
  by ordinary rejections, so a test that "an expired lease is reclaimed" can pass
  by reclaiming a *stranded* row rather than a genuinely crashed one. Land
  BLOCK-1 first so that a `RESERVED` row surviving to expiry means a real crash
  and quarantine is the correct answer for it.
- **BLOCK-8 → BLOCK-5.** BLOCK-5's fix closes a recovered operation by appending
  a terminal receipt. That mechanism is only safe once `reserve` stops reporting
  a terminal *failure* receipt as `ExistingReceipt("already completed")`;
  otherwise recovery would convert an unresolved possible-send into a fake
  success for the next caller of the same idempotency key.
- **BLOCK-3 → BLOCK-9 → BLOCK-4.** Authority, then evidence, then ordering. A
  monotonicity check written before the authority check would be enforced
  against `row.heads` for callers who should never have reached the code at all,
  and the lineage walk in BLOCK-4 reuses the `ledger.get_version` +
  `verify_committed` resolution introduced by BLOCK-9.

Independent of everything else (may be done any time): nothing. All 12 sit on
the graph above.

## 3. Blocking findings

Two shared helpers are introduced by BLOCK-1 and reused by later fixes. Add them
once, in BLOCK-1's commit:

```python
@staticmethod
def _presend(row):
    """Durable proof that no Genie mutation can have been issued for this attempt."""
    return (row.mutation_stage in {None, c.PatchStage.CONFIG_PENDING}
            and 'resume_classification' not in (row.checkpoint or {}))

def _history_raw(self, row, idempotency_key):
    """lookup_request + ambiguity check ONLY. No digest predicate, so _reject can call it."""
    history = self._io(self.facts.lookup_request, row.binding, idempotency_key)
    if history.ambiguous:
        raise CoordinationError('Ambiguous durable request history')
    return history
```

`_history` (`service.py:266`, diff 352) is then re-expressed as `_history_raw`
plus the digest predicate, which is what makes BLOCK-1's `_reject` callable from
inside it without recursion.

---

### BLOCK-1 — A definite pre-send rejection strands the row `RESERVED`+unresolved and escalates to permanent quarantine

**(a) Location.**

| Rejection cause | `service.py` | diff |
|---|---|---|
| grant stale / mismatched / revoked | `admit`, 207–210 | 293–296 |
| preimage belongs to another binding/revision | `admit`, 211–213 | 297–299 |
| preimage not committed | `admit`, 215–216 | 301–302 |
| **reviewed-base mismatch** (the normal drift path) | `admit`, 217–220 | 303–306 |
| create-intent invalid | `admit`, 223–227 | 309–313 |
| approval already consumed / ambiguous | `admit`, 234–236 | 320–322 |
| **idempotency key bound to a different digest** | `_history`, 270–272 | 356–358 |

Escalation path: `_expire` (`service.py:316`, diff 402) → `_quarantine_row`
(`service.py:322`, diff 408). Blocked read path: `observe_exclusively`
(`service.py:441–443`, diff 527–529).

**(b) Why this violates VC/1.0.**

Every row above is a **definite** rejection: the service evaluated a predicate
over evidence it had already read and the answer was "no". `mutation_stage` is
still `None` at all seven sites (`admit`'s CAS at `service.py:237` is what first
sets `CONFIG_PENDING`), so no `AdmissionClaim` was ever issued and M04 provably
cannot have sent anything. Yet none of the seven releases the row. It stays
`state=RESERVED, unresolved=True, holder=<executor>, lease_expires_at=now+60s`
and the caller only gets an exception.

What follows is a two-step brick:

1. Within the lease, every new `reserve` hits `service.py:133–134` (diff 219–220)
   → `OwnershipError('An unresolved attempt already owns this binding')`.
2. After 60 s, the first `_expire` on any entry point sees `RESERVED ∉ {IDLE,
   QUARANTINED}` with an expired lease and calls `_quarantine_row(row, 'Lease
   expired; termination is unproven')`. The row is now `QUARANTINED,
   unresolved=True` — and the recorded reason is false: termination was never in
   question because nothing was ever sent.

`QUARANTINED` has exactly one exit, `recover` (`service.py:336`, diff 422), which
demands trusted termination evidence for the exact attempt *plus* ≥3 stable
post-termination GETs spanning longer than a verified maximum request lifetime
that production deliberately leaves unset (`verified_request_lifetime=None`,
`service.py:53`). So in production the automatic path is closed by design
(correctly — M03§8, C§5.9) and the only remaining exit is the audited human
break-glass path, which BLOCK-5 shows is itself unsafe. The binding is dead.

Violated clauses:

- **C§5.3** — "If reviewed base differs, preserve external state and publish
  `conflicted`; no PATCH." The specified outcome is a *published conflicted
  classification* of the request. `conflicted` is a terminal `OperationStatus`
  (C§2 status list; C§8 `OperationStatus`), not an unresolved attempt.
- **C§5.2** — "same key/different digest → hard conflict". A hard conflict for
  the *request*; nothing licenses forfeiting the *binding*.
- **C§5.7** — "If a crash occurred **after recording send intent**, assume
  possible send." The assume-possible-send obligation is explicitly conditioned
  on recorded send intent. Applying it to an attempt that never reached
  `CONFIG_IN_FLIGHT` inverts the rule's scope.
- **C§5.1** — "an expired occupied row becomes quarantined, never idle" governs a
  row that genuinely holds write authority. A `RESERVED` row that was definitely
  rejected holds none: `assert_owner` refuses its fence (`service.py:181`, "Only
  a reservation can be admitted" / "Reservation … is not write admission").
- **C§7** — the error taxonomy separates `409 conflict/key mismatch/drift` from
  `423 unresolved/quarantine`. This defect converts every 409 into an eventual
  423.
- **TS FM-DRIFT** — "Preserve unexpected state and block write against reviewed
  base", frontend assertion "Captured external version visible before
  adopt/reapply". This is the deadlock: to resolve drift the UI needs a fresh
  external observation; `observe_exclusively` requires `state == IDLE and not
  unresolved` (`service.py:441–443`); the row is neither. **The single most
  common non-error outcome in the system — a concurrent external edit — destroys
  the binding and simultaneously blocks the read needed to repair it.**
- **TS FM-CONCURRENCY** — "Losing request shows conflict, not a success toast."
  Conflict, with the binding still usable.
- **TS F-IDEMPOTENCY** — "same key different digest → hard reject."

**(c) Required fix.** Add a definite-rejection path that publishes the terminal
classification, verifies it durably, and *then* frees the row.

```python
def _reject(self, row, request, message, *, status=c.FactStatus.CONFLICTED):
    """Definite pre-send rejection: publish the terminal fact, then free the binding."""
    if not self._presend(row):
        raise AuthorityUnavailable('Possible send cannot be closed as a definite rejection')
    ref = self._fact(row, request, status)
    if not any(f.event_id == ref.event_id
               for f in self._history_raw(row, request.idempotency_key).facts):
        raise AuthorityUnavailable('Rejection fact not durably verified; row stays unresolved')
    self._release(row)
    raise CoordinationError(message)
```

Replace all seven `self._fact(...)` + `raise` / bare `raise` sites in the table
above with a single `self._reject(row, <request>, '<existing message>')` call.
Note the ordering inside `_reject` is load-bearing and mirrors `finish`'s
existing append-then-read-back-then-release sequence (`service.py:307–314`, diff
393–400): if the fact cannot be proven durable the row must stay unresolved, per
C§5.8 "If projection/fact publication fails, keep unresolved; never erase the
claim."

Do **not** bump generation here. `_release` sets `attempt_id=None`, and `_owned`
compares `fence.attempt_id` against the row, so the rejected fence is already
dead; the next `reserve` bumps generation anyway (`service.py:137`). Minimality
per M03§5 "smallest change".

Do **not** append the rejection as `FactKind.RECEIPT`. It must stay
`FactKind.OPERATION` (the diff's existing kind) so that BLOCK-8's corrected
receipt scan in `reserve` cannot mistake a conflict for a completion.

**(d) RED test — write first.**

`test_definite_presend_rejection_releases_binding_and_publishes_conflict` in
`backend/tests/test_vc_coordination.py`, parameterized
`cause=['reviewed-base', 'different-digest', 'revoked-grant',
'uncommitted-preimage', 'consumed-approval']`.

Asserts, for every cause:

1. the call raises `CoordinationError`;
2. `row.state == CoordinationState.IDLE`, `not row.unresolved`, and
   `row.attempt_id is row.holder is row.approval_id is row.pre_version_id is
   None`;
3. `row.heads == Heads(None, None, None)` and `row.observed_sequence` unchanged —
   the release preserved them;
4. exactly one durable fact exists for that idempotency key, with
   `fact_kind == FactKind.OPERATION`, `status == FactStatus.CONFLICTED`,
   `attempt_id == reservation.fence.attempt_id`, and `request` equal to the
   **rejected** request (not any other request — this is BLOCK-7's property,
   which is why BLOCK-7 lands first);
5. the rejected fence is dead: `renew(reservation.fence)` and
   `assert_owner(reservation.fence)` both raise;
6. **the binding is not bricked**: a fresh `reserve` with a new
   `RequestIdentity` succeeds and returns a `Reservation`;
7. `h.clock.advance(timedelta(seconds=61))` followed by `reserve` does **not**
   produce a `QUARANTINED` row — there is nothing left to expire.

Expected RED symptom (behavioral, not an import error): assertion 2 fails with
`row.state == CoordinationState.RESERVED and row.unresolved is True`; assertion 6
fails with `OwnershipError('An unresolved attempt already owns this binding')`;
assertion 7 fails with `row.state == CoordinationState.QUARANTINED`.

Second RED test, `test_drift_conflict_leaves_binding_observable_and_reusable`
(same file), pins the FM-DRIFT round trip end to end: admit-time reviewed-base
mismatch → `CoordinationError` → `observe_exclusively(binding, executor)`
**succeeds** and yields a lease whose `observed_sequence == 1` → `advance_heads`
records the external observation → a new `reserve` + `admit` against the newly
captured base succeeds. Expected RED: `observe_exclusively` raises
`OwnershipError('Binding busy or executor not target-local; return
stale/checkpoint')`.

**(e) Ordering.** After BLOCK-7 (fact identity must be correct before assertion 4
is written, and before `_reject` appends at a key that would otherwise collide
with the existing conflict fact). Before BLOCK-2 and BLOCK-8.

---

### BLOCK-2 — A crashed `OBSERVER` lease quarantines the binding, so observation-only deployments self-destruct on any worker restart

**(a) Location.** `_expire`, `service.py:316–320` (diff 402–406):

```python
def _expire(self, row):
    if (row.state not in {c.CoordinationState.IDLE, c.CoordinationState.QUARANTINED}
            and (row.lease_expires_at is None or row.lease_expires_at <= self.clock.now())):
        return self._quarantine_row(row, 'Lease expired; termination is unproven')
    return row
```

`CoordinationState.OBSERVING` is absent from the exempt set. The row is put into
`OBSERVING, unresolved=True, lease_expires_at=now+60s` by `observe_exclusively`
(`service.py:438–450`, diff 524–536). `_expire` is reached from `reserve`
(`service.py:128`), `observe_exclusively` (`service.py:439`) and `_owned`
(`service.py:160`), so the first call after 60 s quarantines. Silent-quarantine
compounding: `_quarantine_row` (`service.py:322–330`, diff 408–416) appends a
fact only `if row.active_operation_id is not None`, and an observation lease has
none — so the binding dies with **no durable explanation**.

**(b) Why this violates VC/1.0.**

An observation lease never carried mutation authority. Its fence is a plain
`FenceToken`, not an `AdmissionClaim`, so `assert_owner` rejects it
(`service.py:180–181`: "Reservation/observation is not write admission") and M04
can never PATCH under it. `mutation_stage` is `None`, `admitted_at` is `None`,
`active_operation_id` is `None`. There is no queued Genie mutation and there
never could have been.

Quarantine is nonetheless terminal, and the repair predicate is incoherent for
this case: `_validate_samples` (`service.py:395`, diff 481) demands
post-termination GET *stability* spanning the maximum Genie **request lifetime** —
a bound that exists solely because a mutation may still be queued server-side
(C§5.9). It is asking for evidence about a risk that structurally cannot exist
for a reader. And since production ships `verified_request_lifetime=None`, the
automatic path is closed anyway; the binding can only be freed by an audited
human break-glass, which BLOCK-5 shows is unsafe.

Violated clauses:

- **C§5 Observer protocol** — "acquire a non-mutating observation lease before
  GET"; "**If capture fails, return stale and disable reconcile actions.**"
  Returning stale is the *specified* failure mode for a failed capture.
- **C§5 Observer protocol, final sentence** — "**Observation-only deployments may
  use this coordination subset while write admission is feature-disabled.**" A
  topology that bricks a binding on its first worker restart cannot satisfy this.
  With write admission feature-disabled there is no `recover` caller at all, so
  every restart permanently costs one binding.
- **C§5.7** — again, "if a crash occurred **after recording send intent**". No
  send intent exists for a reader.
- **C§5.1** — "an expired occupied row becomes quarantined, never idle" is scoped
  to a row holding write authority; conflating a read lease with write authority
  is the error.
- **TS F-OPENS** — "Concurrent opens, **slow GET**, active managed checkpoint,
  persistence failure → … observed only; **stale on failure**." A slow GET is
  named explicitly: a >60 s GET is enough to trigger this with no crash at all.
- **TS FM-OUTAGE** — "available history reads continue."
- **M03§6** — "F-OPENS: observed-only updates serialize with mutation
  reservations and cannot regress history" presumes the observer subset stays
  operable.

**(c) Required fix.** An expired `OBSERVING` lease is **reclaimed to IDLE**,
never quarantined.

```python
def _expire(self, row):
    if row.state in {c.CoordinationState.IDLE, c.CoordinationState.QUARANTINED}:
        return row
    if row.lease_expires_at is not None and row.lease_expires_at > self.clock.now():
        return row
    if row.state == c.CoordinationState.OBSERVING:
        return self._reclaim_observer(row)
    return self._quarantine_row(row, 'Lease expired; termination is unproven')

def _reclaim_observer(self, row):
    # Fail closed if the row somehow carries mutation authority.
    if (row.active_operation_id is not None or row.admitted_at is not None
            or row.approval_id is not None or not self._presend(row)):
        raise AuthorityUnavailable('Observation row carries mutation authority; refusing reclaim')
    return self._release(row, generation=row.generation + 1)
```

Two details are load-bearing. The guard in `_reclaim_observer` means a row
mislabelled `OBSERVING` while holding an approval or a stage never gets silently
released — it fails closed instead. The `generation + 1` bump follows C§5.9's
idiom for invalidating outstanding tokens; the stale fence is *also* dead via
`attempt_id=None`, and the RED test asserts that independently so the bump is
belt-and-braces rather than the sole defence.

**Do not** append an audit fact here. There is no `operation_id` to attach one
to, and `contracts.md` §2 constrains `fact_kind` by SQL `CHECK` to a closed list
with no reclaim kind; inventing one would be a VC/1.0 contract change and lies
outside M03's ownership (M03§2 lists "M06 OperationFacts implementation" as
read-only). The reclaim is observable through the row itself.

**(d) RED test — write first.**

`test_expired_observation_lease_is_reclaimed_and_never_quarantines_binding` in
`backend/tests/test_vc_recovery.py`, parameterized
`entry=['reserve', 'observe_exclusively', 'advance_heads']` to cover all three
`_expire` call sites.

Asserts:

1. after `lease = observe_exclusively(...)` then `clock.advance(61s)` then the
   parameterized entry point, `row.state == CoordinationState.IDLE`,
   `not row.unresolved`, `row.attempt_id is None`,
   `row.quarantine_reason is None`;
2. `row.heads` and `row.observed_sequence` are unchanged by the reclaim
   (`observed_sequence == 1`);
3. a fresh `observe_exclusively` succeeds and `newer.observed_sequence >
   lease.observed_sequence`;
4. a fresh `reserve` succeeds — write admission survives an observer crash;
5. the stale lease is dead: `advance_heads(lease.fence, HeadUpdate(uid(), None,
   None, None))` and `assert_owner(lease.fence)` both raise;
6. **no** appended fact has `status == FactStatus.QUARANTINED`;
7. negative control, so the fix is not over-broad: an `ADMITTED` row whose lease
   expires still quarantines, and its `QUARANTINED` fact is still appended.

Expected RED symptom: assertion 1 fails with `row.state ==
CoordinationState.QUARANTINED and row.quarantine_reason == 'Lease expired;
termination is unproven'`; assertions 3 and 4 fail with
`OwnershipError('Binding busy or executor not target-local…')` and
`OwnershipError('An unresolved attempt already owns this binding')`.

Add `test_reclaim_refuses_observing_row_that_carries_mutation_authority`: seed —
via `store.seed`, the M02 fake's deliberate raw-INSERT escape hatch — a row with
`state=OBSERVING` but `approval_id` set, advance past the lease, and assert
`AuthorityUnavailable`, with the row **unchanged**. Expected RED: the row is
released to `IDLE`.

**(e) Ordering.** After BLOCK-1 — before it, a `RESERVED` row is routinely
stranded by ordinary rejections, so a "lease reclaimed" test could pass by
reclaiming a stranded row instead of a genuinely crashed one, and assertion 7's
negative control would be ambiguous.

---

### BLOCK-3 — `advance_heads` accepts a bare `Reservation` fence, letting a never-admitted attempt set the approved and deployed heads

**(a) Location.** `advance_heads`, `service.py:452–476` (diff 538–562). The
authority check is `service.py:453–457` (diff 539–544):

```python
row = self._owned(fence)
observing = row.state == c.CoordinationState.OBSERVING
if update.approved is not None or update.deployed is not None:
    if (observing or not update.authorization_reference
            or not self._io(self.authorize_heads, row.binding, fence, update)):
        raise CoordinationError('Approved/deployed heads require verified policy/deployment evidence')
```

**(b) Why this violates VC/1.0.**

`_owned` (`service.py:157`, diff 243) accepts **any** `FenceToken` matching
`(binding_revision, attempt_id, generation)` with a live lease and
`unresolved=True`. A row in state `RESERVED` — an attempt that never passed
through `admit`, so `approval_id`, `approval_digest`,
`expected_base_fingerprint`, `pre_version_id` and `admitted_at` are all still
NULL — satisfies `_owned`, is not `OBSERVING`, and therefore reaches the head
write. `advance_heads` is the **only** authority-bearing entry point that does not
call `assert_owner`; compare `checkpoint` (`service.py:410`, diff 496) and
`finish` (`service.py:288`, diff 374), which both open with
`self.assert_owner(claim)`.

The `authorize_heads` callback is not a substitute. It receives `(binding, fence,
update)` and no row state, so a policy implementation cannot tell a reservation
from a claim without re-deriving M03's state machine — which M03§7 forbids
consumers from doing ("Consumers mock this Protocol, never duplicate CAS SQL").

Violated clauses:

- **M03§3** — "`admit(reservation, committed_preimage_or_create_intent,
  AuthorizationGrant) -> AdmissionClaim` is **the sole write-authorizing
  transition**" and "`reserve` returns no PATCH capability." Writing the governed
  head columns is an authority-bearing write.
- **C§5.8** — "observer changes observed only; **approved requires policy
  authorization**; deployed requires verified deployment." Policy authorization
  presupposes an authorized operation; a reservation has no `AuthorizationGrant`
  bound to the row at all.
- **C§5.4** — `admit` is the step that "revalidates authorization" and binds the
  approval id/digest. Bypassing it sets the approved head with no revalidated
  authorization in existence.
- **C§8** — `BindingStatus.heads` feeds every drift decision and "Badge `In sync`
  derives from approved/deployed policy equality." An unadmitted attempt can make
  the UI report approved-and-deployed at a version nobody approved.
- **TS FM-CONCURRENCY** — "one admitted sequence." Heads are the serialized
  authority; writing them outside admission breaks that property.

The diff's own test conceals the gap. `test_observer_cannot_regress_or_approve_heads`
exercises approved/deployed only through `claim = admit(h)` (diff 1539–1544) and
its negative cases only cover the *observation* lease (diff 1515–1518). The
`RESERVED` case sits exactly between the two and is untested.

**(c) Required fix.** Require a verified admission for any non-observer head
write.

```python
def advance_heads(self, fence, update):
    row = self._owned(fence)
    if update.observed is None and update.approved is None and update.deployed is None:
        raise CoordinationError('Empty head update')
    observing = row.state == c.CoordinationState.OBSERVING
    if not observing:
        if not isinstance(fence, c.AdmissionClaim):
            raise OwnershipError('Head updates require an observation lease or an admission claim')
        self.assert_owner(fence)          # re-verifies ADMITTED + request/approval/preimage vs row
    if update.approved is not None or update.deployed is not None:
        if (observing or not update.authorization_reference
                or not self._io(self.authorize_heads, row.binding, fence, update)):
            raise CoordinationError('Approved/deployed heads require verified policy/deployment evidence')
    ...
```

`assert_owner` (`service.py:179–190`) supplies precisely the missing
revalidation: `isinstance(claim, AdmissionClaim)`, `row.state == ADMITTED`, and
claim request / approval id+digest / preimage all equal to the durable row. The
explicit `isinstance` check first yields a precise `OwnershipError` instead of
`assert_owner`'s generic message; both subclass `CoordinationError`, so callers
are unaffected.

The empty-update guard is included because `advance_heads` releases an
observation lease on completion (`service.py:474–475`, diff 560–561) — without
it, an all-`None` update silently consumes a lease and performs a no-op CAS.

**(d) RED test — write first.**

`test_reservation_fence_cannot_advance_any_head` in
`backend/tests/test_vc_coordination.py`, parameterized
`head=['observed', 'approved', 'deployed']`.

Asserts:

1. with `h.authorize_heads.return_value = True` — proving the policy callback is
   *not* the missing gate, i.e. the bug is not "policy said no" — every
   `advance_heads(reservation.fence, HeadUpdate(...))` raises `CoordinationError`;
2. `h.store.read(...).heads == Heads(None, None, None)`: no partial write;
3. for the approved/deployed cases, `h.authorize_heads.assert_not_called()` — the
   authority check must precede the policy callback, so no policy evaluation is
   even attempted;
4. `advance_heads(lease.fence, HeadUpdate(None, None, None, None))` raises
   (empty update) and the observation lease is **still held** afterwards
   (`row.state == OBSERVING`);
5. positive control: after `admit`, the same approved/deployed `HeadUpdate` with
   `authorize_heads` true **succeeds** — the fix does not over-block the
   legitimate path;
6. `advance_heads(replace(claim, request=replace(h.request, request_digest='f'*64)),
   ...)` raises, proving `assert_owner`'s durable-row comparison is now on the
   path.

Expected RED symptom: assertion 1 fails because
`advance_heads(reservation.fence, HeadUpdate(None, v, v, 'ref'))` **returns**
`Heads(None, v, v)`, and assertion 2 then shows the row's approved and deployed
columns populated by an attempt that never held an `AdmissionClaim`.

**(e) Ordering.** Independent of BLOCK-1/2. **Before BLOCK-9 and BLOCK-4** — the
lineage and evidence checks belong behind the authority check, and writing them
first would enforce monotonicity for callers who should never have reached the
code.

---

### BLOCK-4 — Approved and deployed heads can silently REGRESS to an older version

**(a) Location.** `advance_heads`, `service.py:470–472` (diff 556–558):

```python
heads = c.Heads(update.observed or row.heads.observed,
                update.approved or row.heads.approved,
                update.deployed or row.heads.deployed)
```

Contrast the `observed` head, which *does* get a lineage guard at
`service.py:458–469` (diff 546–555): it requires `context.parent_version_id ==
row.heads.observed`, i.e. the new observation must be a direct child of the
current observed head. **`approved` and `deployed` get no guard whatsoever.**

**(b) Why this violates VC/1.0.**

Whatever version id the caller supplies is written, provided `authorize_heads`
returns true. Three distinct regressions follow:

1. `approved` can be moved **backwards** to an ancestor — an unconditional
   in-process rollback of the approval head.
2. `deployed` can be set **ahead of** `approved`, claiming a deployment of
   something never approved.
3. Either can be set to a version id off this binding's lineage entirely
   (BLOCK-9 covers the missing commit verification; the ordering failure is
   this finding).

The damage is silent and downstream: M05's `DriftService.classify(heads,
versions, reachability)` (C§4) and the `In sync` badge (C§8) both read these
columns, so a regressed approved head makes a real divergence classify as
`clean`. The row keeps no trace — the old value is simply overwritten and there
is no head-change fact.

Violated clauses:

- **C§5.10** — "Compensation is a new governed operation linked to the original,
  permitted only with bound preauthorization and fresh live equality to the
  recorded post-stage. If it drifted, preserve and block. **No in-process
  unconditional rollback.**" Moving `approved` backwards *is* that rollback.
- **C§5.8** — "Generation-conditioned head updates … approved requires policy
  authorization; deployed requires **verified deployment**." A deployed head
  leading the approved head has no verified deployment behind it. The operation
  is named `advance_heads`.
- **TS F-OPENS** — "**No head regression**/duplicate observation/mislabeling."
- **M03§6 acceptance** — "F-OPENS: observed-only updates serialize with mutation
  reservations **and cannot regress history**."
- **C§8** — "Short IDs are display labels, **never** monotonic version numbers or
  authoritative ordering." This is the binding design constraint on the fix:
  monotonicity must be proven by committed **lineage**, never by comparing ids or
  timestamps.

**(c) Required fix.** Enforce monotonicity along committed lineage, reusing the
`ledger.get_version` resolution BLOCK-9 introduces.

```python
def _advances(self, binding, current, candidate):
    """True iff candidate is current, or a descendant of current, by committed lineage."""
    if current is None:
        return True
    seen, node = set(), candidate
    while node is not None and node not in seen:
        if node == current:
            return True
        seen.add(node)
        version = self._io(self.ledger.get_version, binding, node)
        if version is None or version.context.binding != binding:
            raise CoordinationError('Head lineage is not committed to this binding')
        node = version.context.parent_version_id
    return False
```

In `advance_heads`, after the BLOCK-3 authority gate and the BLOCK-9 evidence
check, for each of `approved`/`deployed` that is not `None`:

```python
if not self._advances(row.binding, getattr(row.heads, name), value):
    raise CoordinationError(f'{name} head cannot regress or leave the committed lineage')
```

then the C§5.8 cross-head rule:

```python
final_approved = row.heads.approved if update.approved is None else update.approved
final_deployed = row.heads.deployed if update.deployed is None else update.deployed
if final_deployed is not None and not self._advances(row.binding, final_deployed, final_approved):
    raise CoordinationError('Deployed head must not lead the approved head')
```

While here, replace the three `x or y` expressions with explicit `is None` tests.
`or` cannot express "clear a head" and treats any falsy value as absent; harmless
for UUID strings today, so the `or` idiom alone is filed as **NB-3**, but the
line is being rewritten anyway.

The `seen` cycle guard is not defensive padding: `parent_version_id` comes from
durable data that Delta does not constrain (`contracts.md` §2, "No reliance on
informational PK/UNIQUE constraints"), so a corrupted lineage cycle must fail
closed rather than spin forever inside a held CAS window.

**(d) RED test — write first.**

`test_approved_and_deployed_heads_cannot_regress` in
`backend/tests/test_vc_coordination.py`.

Setup note: the fixture must drive `h.ledger.get_version` with a **`side_effect`
keyed by version_id**, building a real chain `v1 → v2 → v3`. The diff's existing
test uses `return_value` (diff 1523, 1535), which structurally cannot express a
lineage — that is precisely why the regression was never caught.

Asserts:

1. advance approved and deployed to `v2` with `authorize_heads` true (baseline);
2. `advance_heads(claim, HeadUpdate(None, v1, None, 'ref'))` — an ancestor —
   raises `CoordinationError`;
3. the row's heads are byte-identical to the pre-attempt heads, and
   `h.store.compare_and_swap.call_count` did not increase: the CAS never ran, so
   there is no partial write;
4. `HeadUpdate(None, None, v3, 'ref')` with approved still at `v2` raises
   ("deployed must not lead approved");
5. a version on an unrelated chain — whose parent walk never reaches the current
   head — raises;
6. a lineage **cycle** (`side_effect` returning `v_a.parent = v_b`,
   `v_b.parent = v_a`) raises `CoordinationError` and terminates;
7. a genuine forward advance `v2 → v3` for both heads still succeeds.

Expected RED symptom: assertion 2 fails because the call **returns**
`Heads(v2, v1, v2)` and assertion 3 then shows the approved column regressed to
`v1`; assertion 4 fails because deployed advances to `v3` while approved stays at
`v2`; assertion 6 fails by non-termination or `StopIteration` from the mock
rather than a typed `CoordinationError`.

**(e) Ordering.** After BLOCK-3 (authority first) and after BLOCK-9 (the
`get_version` + `verify_committed` resolution this walk reuses). Last of the
three head fixes.

<!-- SECTION-3-BODY -->

## 4. Non-blocking findings

<!-- SECTION-4-BODY -->
