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

### BLOCK-5 — `recover` releases the binding without closing the operation, so the same idempotency key can be admitted a second time and M04 re-issues the PATCH

**(a) Location.** `recover`, `service.py:336–368` (diff 422–454). The defect is
the tail of the method, `service.py:359–368` (diff 445–454):

```python
ref = self._fact(row, request, c.FactStatus.CONFLICTED,
                 evidence=evidence, kind=c.FactKind.RECOVERY)
history = self._history(row, request)
if not any(f.event_id == ref.event_id for f in history.facts):
    raise AuthorityUnavailable('Recovery audit publication not verified')
released = self._release(row, generation=row.generation + 1)
...
return c.RecoveryResult(binding, c.OperationStatus.CONFLICTED, fence, False, (ref.event_id,))
```

The only durable fact recovery writes for the request is
`kind=FactKind.RECOVERY`. `_release` (`service.py:275`, diff 361) then clears
`active_operation_id`, `idempotency_key` and `request_digest`, so the row retains
no memory of the operation at all. The re-admission path is `reserve`
(`service.py:147–154`, diff 233–240), whose only terminal-completion test is
`receipts = [f for f in history.facts if f.fact_kind == c.FactKind.RECEIPT]`.

**(b) Why this violates VC/1.0.**

Recovery is reached only from `QUARANTINED`, i.e. only for an attempt whose send
outcome is *unknown* — `_owned(..., allow_quarantine=True)` plus the
`row.state != QUARANTINED` guard at `service.py:341` (diff 427) make that
structural. The whole point of the ≥3-stable-GET evidence is to license
**closing** that attempt, not to license retrying it. But after `recover`
returns:

1. the row is `IDLE, unresolved=False` with a bumped generation;
2. the request history for `(binding, idempotency_key)` contains OPERATION and
   RECOVERY facts and **no RECEIPT**;
3. so a fresh `reserve(binding, RequestIdentity(new_operation_id,
   same_idempotency_key, same_request_digest), executor)` passes `_history`'s
   digest predicate (the digest is unchanged — that is the *whole* definition of
   a retry of the same request), finds `receipts == []`, and returns a
   `Reservation`;
4. `admit` then issues a fresh `AdmissionClaim`, and M04 sends the config PATCH
   for the second time — for an operation whose first PATCH may already be
   queued server-side.

The generation bump does not help. Generation fences *outstanding tokens* from
the dead attempt; it says nothing about whether a *new* attempt may be admitted
for the same request. The two are different obligations and the diff satisfies
only the first.

Violated clauses:

- **C§5.5** — "Old consumption survives later row reuse; **same-operation
  recovery can finish publication but does not grant a fresh mutation
  attempt.**" This is the clause verbatim. `recover` finishes publication
  (`service.py:355`, diff 441) and then grants exactly the fresh mutation
  attempt the sentence forbids.
- **C§5.7** — "matching GET is observation only, **not** permission to replay or
  run the second PATCH." The recovery samples *are* matching GETs. Converting
  three of them into a re-admission is the prohibited inference, merely deferred
  by one call.
- **C§5.2** — "same key/same completed digest → existing receipt, no mutation."
  A recovered attempt is a completed request in the only sense available: no
  further mutation is licensed for it. With no receipt on the key, the contract's
  own stop condition is missing.
- **C§5.9** — "classify against checkpoint, **record recovery facts**, bump
  generation." Recording the recovery *audit* is necessary but not sufficient;
  the operation's own status must be terminal, and C§2 lists `conflicted` as
  a terminal `OperationStatus` reachable for exactly this case.
- **TS FM-RECOVERY** — "Timeout/crash quarantines, **no replay**; exact
  termination + stable reads or audited human action", frontend assertion "no
  retry-PATCH control". A recovery that re-opens the key is a retry-PATCH
  control with extra steps.
- **TS F-DESCRIPTION** — "**No second-PATCH replay**; partial/unverified and
  quarantine preserved."
- **TS F-CRASH-STAGES** — "no premature release/replay."
- **M03§6** — "FM-OUTAGE/FM-RECOVERY and F-APP-CRASH/F-DESCRIPTION: failure
  cannot authorize replay or takeover."

The audited human path makes this worse, not better: `test_unknown_request_lifetime_disables_auto_clear`
(diff 1658–1690) exercises `recover` with `human_reason='Orphan inspection
complete; **reconcile under new governed request**'`. The evidence string
promises a *new governed request*; the code delivers the ability to re-run the
*old* one.

**(c) Required fix.** Recovery closes the operation. Append a terminal receipt
for the request alongside the recovery audit, prove both durable, then release.

```python
ref = self._fact(row, request, c.FactStatus.CONFLICTED,
                 evidence=evidence, kind=c.FactKind.RECOVERY)
receipt = self._fact(row, request, c.FactStatus.CONFLICTED,
                     evidence=evidence, kind=c.FactKind.RECEIPT)
history = self._history_raw(row, request.idempotency_key)
if not {ref.event_id, receipt.event_id} <= {f.event_id for f in history.facts}:
    raise AuthorityUnavailable('Recovery audit/receipt publication not verified; row stays unresolved')
released = self._release(row, generation=row.generation + 1)
...
return c.RecoveryResult(binding, c.OperationStatus.CONFLICTED, fence, False,
                        (ref.event_id, receipt.event_id))
```

Three details are load-bearing.

- The receipt status is `CONFLICTED`, never `CONFIRMED`/`NOOP`. Recovery never
  proves the mutation landed; it proves the *executor* is dead and live state is
  stable. C§5.9's classification is of the checkpoint, not of success.
- `_history_raw` (BLOCK-1's helper), not `_history`. `_history` re-runs the
  digest predicate and would append a *third* fact on mismatch from inside the
  release path.
- The two appends must land at **different `event_key`s**. Under the diff's key
  template (`service.py:116`, diff 202) both facts are
  `{operation_id}:{attempt_id}:{row_version}:conflicted` — a straight collision.
  BLOCK-7 puts `fact_kind` into the key; that is precisely why BLOCK-7 must land
  first and why this fix cannot be attempted before it.

Do **not** attempt to keep the operation open "pending reconciliation". C§5.5
and C§5.10 route the follow-up through a *new governed operation* with its own
idempotency key and its own fresh approval; nothing in M03 needs to hold state
for it.

**(d) RED test — write first.**

`test_recovery_closes_the_operation_and_forbids_a_second_admission` in
`backend/tests/test_vc_recovery.py`, parameterized
`path=['automatic', 'human']` so both exits from `QUARANTINED` are covered
(reuse `recovery_evidence` plus the `verified_request_lifetime` /
`authorize_human_recovery` setup from diff 1618 and 1668–1673).

Asserts, for both paths:

1. `result = recover(...)` returns `status == OperationStatus.CONFLICTED` and
   `not result.unresolved` (unchanged baseline);
2. the durable history for `claim.request.idempotency_key` now contains **both**
   a `FactKind.RECOVERY` fact and a `FactKind.RECEIPT` fact, each with
   `status == FactStatus.CONFLICTED`, `attempt_id == claim.attempt_id`, and
   `post_version_id is None`;
3. their `event_key`s differ and `not history.ambiguous` — the collision guard;
4. **the operation cannot be admitted again**: `reserve(binding,
   replace(h.request, operation_id=uid()), executor)` — same idempotency key,
   same digest — raises `ExistingReceipt`, and
   `caught.value.receipt.operation_id == claim.request.operation_id` and
   `caught.value.receipt.status == FactStatus.CONFLICTED`;
5. no `AdmissionClaim` is obtainable for that key: the `reserve` in 4 never
   returns a `Reservation`, and `h.store.read(...).state ==
   CoordinationState.IDLE` with `not row.unresolved` afterwards (the
   `ExistingReceipt` path still releases, per BLOCK-8's corrected form);
6. the binding is **not** bricked for genuinely new work: `reserve` with a
   *different* `idempotency_key` succeeds and `admit` returns an
   `AdmissionClaim`;
7. publication failure keeps the row unresolved: with
   `h.facts.lookup_request.side_effect = OSError('receipt read-back unavailable')`
   after the appends, `recover` raises `AuthorityUnavailable` and
   `h.store.read(...).state == CoordinationState.QUARANTINED` — recovery must
   never release on unverified publication.

Expected RED symptom (behavioral): assertion 2 fails with
`[f.fact_kind for f in history.facts] == [FactKind.OPERATION,
FactKind.APPROVAL_CONSUMED, FactKind.RECOVERY]` — no receipt; assertion 4 fails
because `reserve` **returns a `Reservation`** instead of raising
`ExistingReceipt`, and the follow-on `admit` in the same test returns a live
`AdmissionClaim` for a request whose first PATCH was never proven unsent.

**(e) Ordering.** After BLOCK-8 — BLOCK-5's whole mechanism is "close the
operation with a terminal receipt", which is unsafe while `reserve` still reports
a terminal *failure* receipt as `ExistingReceipt('already completed')`; a
`CONFLICTED` receipt written before BLOCK-8 would be read by the next caller as
a success. After BLOCK-10 (recovery must not release while the consumption /
termination columns are untruthful) and transitively after BLOCK-7 (event-key
identity) and BLOCK-1. **Before BLOCK-11**, which changes *which*
classification this receipt carries.

---

### BLOCK-6 — `finish` quarantines on any non-terminal result, including definite pre-send failures, so a rejected-before-send operation costs the binding

**(a) Location.** `finish`, `service.py:288–314` (diff 374–400). The two guards
at `service.py:290–299` (diff 376–385):

```python
if (row.mutation_stage in {c.PatchStage.CONFIG_IN_FLIGHT, c.PatchStage.DESCRIPTION_IN_FLIGHT}
        or result.unresolved or result.status in {
            c.OperationStatus.APPLIED_UNVERIFIED, c.OperationStatus.APPLIED_PARTIAL}):
    self._quarantine_row(row, 'Possible send remains unresolved; GET cannot prove termination')
    raise CoordinationError('In-flight or ambiguous result cannot release the attempt')
if (result.operation_id != row.active_operation_id or result.preimage != claim.preimage
        or result.unresolved or result.status not in {
            c.OperationStatus.CONFIRMED, c.OperationStatus.NOOP,
            c.OperationStatus.CONFLICTED, c.OperationStatus.FAILED}):
    raise CoordinationError('Not a resolved terminal result')
```

The first guard is correct and must be kept. The defect is the **second** guard
and everything it reaches: it raises without releasing and without quarantining,
leaving `state=ADMITTED, unresolved=True`. Same for the postimage guards at
`service.py:300–306` (diff 386–392) and the `_publish_consumption` /
receipt-verification failures at `service.py:307–313` (diff 393–399).

**(b) Why this violates VC/1.0.**

`finish` is the *only* release path for an `ADMITTED` row. Every rejection above
therefore strands the attempt exactly as BLOCK-1 stranded `RESERVED` rows, with
the same two-step escalation: `OwnershipError` for 60 s, then `_expire` →
`_quarantine_row('Lease expired; termination is unproven')` → dead binding whose
only exits are the closed automatic path and the break-glass path.

The critical case is a **proven pre-send failure**. M04's `MutationGate.execute`
can legitimately return `OperationStatus.FAILED, unresolved=False` for a request
it rejected *before* issuing any HTTP call — a rendered payload that failed local
validation, a transport pre-check, a `403` on the pre-flight GET. In that case
`row.mutation_stage` is still `CONFIG_PENDING` and `row.checkpoint` carries no
`resume_classification`: durable state *proves* no mutation was issued. That
result is in the accepted terminal set, so it reaches the receipt path — but if
`result.postimage is None` and the status is `FAILED` it survives
`service.py:300–306`, so this particular case does release. The stranding cases
are the ones adjacent to it:

- `result.status == CONFIRMED` with `postimage is None` →
  `'Successful completion requires committed postimage'` (`service.py:305`, diff
  391). A buggy or partially-failed M04 caller costs the binding.
- `result.preimage != claim.preimage`, or `result.operation_id !=
  row.active_operation_id` → `'Not a resolved terminal result'`. A *caller
  mistake* — the most likely error in the system — is punished by permanent
  quarantine of the binding rather than by a rejected call.
- `result.status in {COMPENSATION_ATTEMPTED, COMPENSATED, REQUESTED,
  PREIMAGE_CAPTURED, APPLY_ATTEMPTED, QUARANTINED}` → same.
- `_publish_consumption` raising `AuthorityUnavailable`, and the
  `'Terminal receipt publication not verified'` raise: these two are **correct**
  to leave unresolved (C§5.8) and must not be changed.

So `finish` currently makes exactly the wrong distinction. It separates
"in-flight" from "everything else", where VC/1.0 separates **proven pre-send**
from **possible-send**, and within possible-send separates *durable-publication
failure* (stay unresolved) from *caller/argument error* (reject, keep the
binding).

Violated clauses:

- **C§5.7** — "If a crash occurred **after recording send intent**, assume
  possible send." Same scope inversion as BLOCK-1: the assume-possible-send
  obligation is conditioned on recorded send intent, and a `CONFIG_PENDING` row
  has none.
- **C§5.8** — "Publish all required terminal/consumption facts and confirm them
  before clearing active claim. **If projection/fact publication fails, keep
  unresolved; never erase the claim.**" This clause licenses staying unresolved
  for *publication* failure only. The diff applies it to argument validation.
- **C§7** — `409 conflict/key mismatch` vs `423 unresolved/quarantine`. A
  mismatched `OperationResult` is a 409-class caller error; the diff makes it a
  423-class binding loss.
- **C§5.3** — the no-op path: "Persist no-op receipt when all three fingerprints
  match; **no write-authorizing admission necessary**, but authorization,
  idempotency and guarded finalization still apply." A `NOOP` finalization must
  be a clean release, and it is reachable here only through the postimage guard.
- **TS F-CRASH-STAGES** — "No PATCH without committed bound evidence, **no
  premature release/replay**." Symmetrically: no *permanent* non-release either.
- **TS FM-OUTAGE** — "available history reads continue"; a stranded `ADMITTED`
  row blocks `observe_exclusively` at `service.py:441–443` (diff 527–529), the
  same read deadlock BLOCK-1 documents.

**(c) Required fix.** Release only on **proven pre-send** failure; keep every
possible-send and every publication failure unresolved; reject caller errors
without touching row state.

Reorder `finish` so the argument checks that cannot depend on row mutation state
run first and raise before anything is written:

```python
def finish(self, claim, result):
    self.assert_owner(claim)
    row = self._owned(claim)
    if (result.operation_id != row.active_operation_id
            or result.preimage != claim.preimage):
        raise CoordinationError('Result does not identify this admitted operation')
    if (row.mutation_stage in {c.PatchStage.CONFIG_IN_FLIGHT, c.PatchStage.DESCRIPTION_IN_FLIGHT}
            or result.unresolved or result.status in {
                c.OperationStatus.APPLIED_UNVERIFIED, c.OperationStatus.APPLIED_PARTIAL}):
        self._quarantine_row(row, 'Possible send remains unresolved; GET cannot prove termination')
        raise CoordinationError('In-flight or ambiguous result cannot release the attempt')
    if result.status not in {c.OperationStatus.CONFIRMED, c.OperationStatus.NOOP,
                             c.OperationStatus.CONFLICTED, c.OperationStatus.FAILED}:
        if self._presend(row):
            return self._reject(row, claim.request, 'Non-terminal result on a proven pre-send attempt',
                                status=c.FactStatus.FAILED)
        raise CoordinationError('Not a resolved terminal result')
    if result.postimage is not None:
        if ((result.postimage.binding_id, result.postimage.binding_revision) !=
                (row.binding.binding_id, row.binding.binding_revision)
                or not self._io(self.ledger.verify_committed, result.postimage)):
            raise CoordinationError('Terminal observation not committed to this binding')
    elif result.status in {c.OperationStatus.CONFIRMED, c.OperationStatus.NOOP}:
        if self._presend(row):
            return self._reject(row, claim.request, 'Successful completion requires committed postimage',
                                status=c.FactStatus.FAILED)
        raise CoordinationError('Successful completion requires committed postimage')
    ...  # publish consumption, append receipt, read back, release — unchanged
```

`_reject` is BLOCK-1's helper, unchanged: it re-asserts `_presend`, appends the
terminal fact, proves it durable, releases, and raises. The `status=FactStatus.FAILED`
override is deliberate — a pre-send rejection at `finish` did not conflict with
anything external, and C§2's status list distinguishes `failed` from
`conflicted`. The fact stays `FactKind.OPERATION` (BLOCK-1's rule) so BLOCK-8's
receipt scan cannot read it as a completion.

Two things must **not** change. The in-flight/`unresolved`/`applied_*`
quarantine stays first among the state-dependent checks and stays a quarantine —
that is C§5.7 operating in its correct scope. And `_publish_consumption`
failure plus `'Terminal receipt publication not verified'` continue to raise with
the row unresolved — C§5.8 explicitly requires it, and
`test_checkpoint_normal_sequence_and_flush_failure_retains_claim` (diff
1492–1503) pins it.

**(d) RED test — write first.**

`test_finish_releases_only_on_proven_presend_failure` in
`backend/tests/test_vc_coordination.py`, parameterized
`case=['confirmed-no-postimage', 'wrong-preimage', 'wrong-operation-id',
'compensation-attempted', 'noop-no-postimage']`.

Asserts:

1. every case raises `CoordinationError`;
2. for the three cases where `_presend(row)` holds — `mutation_stage` is
   `CONFIG_PENDING` and no `resume_classification` in `row.checkpoint`, asserted
   directly on the row before the call — the row afterwards is
   `state == CoordinationState.IDLE`, `not row.unresolved`, `attempt_id is None`,
   and `quarantine_reason is None`;
3. `row.heads` and `row.observed_sequence` are unchanged by the release;
4. exactly one new durable fact for that idempotency key, with
   `fact_kind == FactKind.OPERATION` (**not** `RECEIPT`),
   `status == FactStatus.FAILED`, and `attempt_id == claim.attempt_id`;
5. the binding is reusable: a fresh `reserve` with a new `RequestIdentity`
   returns a `Reservation`;
6. `clock.advance(61s)` then `reserve` does **not** yield `QUARANTINED`;
7. **negative control — caller error on a possible-send row is not released**:
   after `checkpoint(claim, CONFIG_IN_FLIGHT, ...)`, the same
   `'wrong-preimage'` result raises and the row is **still**
   `unresolved=True` (quarantined by the in-flight guard, not released);
8. **negative control — publication failure still strands, deliberately**: with
   `h.facts.verify_flush.side_effect = lambda _: False`, a fully valid
   `CONFIRMED` + committed postimage result raises `AuthorityUnavailable` and
   `h.store.read(...).unresolved` is `True`. The fix must not widen the release
   path to publication failures.

Expected RED symptom: assertion 2 fails with `row.state ==
CoordinationState.ADMITTED and row.unresolved is True`; assertion 5 fails with
`OwnershipError('An unresolved attempt already owns this binding')`; assertion 6
fails with `row.state == CoordinationState.QUARANTINED` and
`row.quarantine_reason == 'Lease expired; termination is unproven'`. Assertions
7 and 8 pass before the fix and must still pass after it — they are the
over-reach guards.

**(e) Ordering.** After BLOCK-1 (`_reject` and `_presend` are introduced there
and reused verbatim here) and after BLOCK-10 (`_presend` reads
`row.mutation_stage` and `row.checkpoint`; BLOCK-10 is what makes the adjacent
`approval_consumption_published` column truthful, so a `finish` fix landed first
would be verified against a column that lies). Transitively after BLOCK-7.
**Before BLOCK-12**, which scopes the stage machine that `_presend` interrogates.

---

### BLOCK-7 — `_fact` attributes a losing request's conflict to the *winner's* attempt and identity, and the `event_key` template collides

**(a) Location.** `_fact`, `service.py:108–125` (diff 194–211). Three distinct
defects in one helper.

*(i) Rejected-attempt attribution.* Every field that identifies *who* the fact
is about is read from `row`, never from the caller:

| `service.py` | diff | field | source |
|---|---|---|---|
| 116 | 202 | `event_key` | `row.attempt_id`, `row.row_version` |
| 119 | 205 | `requester_id` | `row.holder or 'coordination'` |
| 120–121 | 206–207 | `actor` | `ActorContext(row.holder, ..., row.executor_kind)` |
| 122 | 208 | `attempt_id`, `generation` | `row.attempt_id`, `row.generation` |
| 123 | 209 | `pre_version_id` | `row.pre_version_id` |
| 124 | 210 | `approval_id`, `approval_digest` | `row.approval_id`, `row.approval_digest` |

The one call site where `row` is *not* the subject is the CAS-loser path in
`reserve`, `service.py:144–146` (diff 230–232):

```python
except OwnershipError:
    self._fact(row, operation, c.FactStatus.CONFLICTED)
    raise
```

`row` here is the row as read *before* the losing CAS — but after the winner
committed, the losing thread's `_cas` calls `_row` again (`service.py:94`, diff
180) and its `AuthorityUnavailable`/`OwnershipError` propagates with the local
`row` variable still bound to the pre-race read. Worse, in the `state != IDLE`
branch at `service.py:133–134` (diff 219–220) the raise happens *after* `_expire`
and `_row` have already returned the **winner's occupied row**. So the loser's
conflict fact carries the winner's `attempt_id`, `holder`, `generation`,
`approval_id` and `pre_version_id`, with only `request` identifying the loser.

*(ii) `event_key` collisions.* `service.py:116` (diff 202):

```python
key = f'{request.operation_id}:{row.attempt_id}:{row.row_version}:{status.value}'
```

`fact_kind` is absent. Two facts with the same operation/attempt/row_version and
the same status but different kinds collide. This is not hypothetical:

- BLOCK-5's fix appends `RECOVERY` + `RECEIPT`, both `CONFLICTED`, both from the
  same `row` — identical keys.
- `recover` already appends `RECOVERY(CONFLICTED)` at `service.py:359` (diff 445)
  on a row that, if it reached quarantine through `_quarantine_row`, is the same
  `row_version`-adjacent lineage as its `QUARANTINED` fact.
- BLOCK-1's `_reject` appends `OPERATION(CONFLICTED)` where the diff already
  appends `OPERATION(CONFLICTED)` from `_history` (`service.py:271`, diff 357) —
  same key, and if both fire the idempotency key is permanently poisoned.

The collision is load-bearing because `lookup_request` derives `ambiguous` from
key uniqueness. The M02 fake does it explicitly (`FakeFactStore` + the
integration adapter at diff 778: `len({f.event_key for f in matching}) !=
len(matching)`), and once `ambiguous` is `True`, `_history` raises
`'Ambiguous durable request history'` **forever** for that key — from `reserve`,
from `admit`, from `finish`. One collision bricks the request.

*(iii) `row_version` in the key is not stable.* `_cas` increments `row_version`
on every write, so the "same" logical fact appended before and after any
unrelated CAS gets two different keys — the opposite failure: a retried
ambiguous append cannot find its own prior key, which is exactly the query C§2
"Grant boundaries" requires ("Retrying an ambiguous append first queries its
key").

**(b) Why this violates VC/1.0.**

- **C§2 (M06 fact schema)** — "Logical uniqueness of event/version/approval
  identifiers is enforced by serialized protocol plus read-back and
  **deterministic event keys**, not unenforced Delta keys." A key template that
  omits `fact_kind` is not deterministic-per-fact; it is deterministic-per-
  collision.
- **C§2** — "Retrying an ambiguous append first queries its key; inconsistent
  duplicates block admission." Both halves are defeated: `row_version` makes the
  key unqueryable across a CAS, and the collision *is* the inconsistent duplicate
  that blocks admission.
- **C§2** — "Read views may deduplicate **byte-identical** retry facts without
  mutating history." Two facts sharing a key with different `fact_kind`s are not
  byte-identical, so no reader may deduplicate them.
- **C§5.1** — "CAS losers return conflict." The conflict must be the loser's
  conflict. A fact naming the winner's attempt is not a record of the loser's
  rejection; it is a second, false record of the winner.
- **C§6 / C§2** — `requester_id` and `actor` are audit identity fields.
  Attributing a rejection to a principal that did not make the request is an
  audit-integrity failure, and `ActorContext` is described in TS as
  "authenticated identity" — "A supplied approver name is ignored/rejected in
  favor of authenticated identity" cuts both ways.
- **TS FM-CONCURRENCY** — "Two requests → one admitted sequence, one
  conflict/quarantine fact", frontend "Losing request shows conflict, not a
  success toast." The UI resolves a request by `operation_id` + `attempt_id`; a
  loser whose fact carries the winner's `attempt_id` cannot be rendered as the
  loser.
- **TS F-IDEMPOTENCY** — "same SP different attempt": the whole row exists to
  prove attempts are distinguishable. `_fact` erases the distinction.
- **M03§6** — "FM-CONCURRENCY: one admitted sequence, **losing request gets
  durable conflict/quarantine fact**."

The diff's own tests cannot catch (i) because both contenders in
`test_two_concurrent_reservations_have_exactly_one_winner` (diff 1193–1233) share
`h.request` and `h.executor` — the loser and winner are indistinguishable by
construction. The integration `assert_conflict` (diff 960–965) checks
`history.facts[0].request == contender.request`, which passes because `request`
*is* the caller's; it never asserts on `attempt_id` or `actor`.

**(c) Required fix.** Make the fact's subject explicit, put `fact_kind` in the
key, and drop `row_version`.

```python
def _fact(self, row, request, status, *, evidence=None, kind=c.FactKind.OPERATION,
          post_version_id=None, subject=None):
    """`subject` names the attempt this fact is ABOUT; None means `row`'s own attempt."""
    stage = row.mutation_stage or c.PatchStage.CONFIG_PENDING
    subject = subject or c.AttemptSubject(row.attempt_id, row.generation, row.holder,
                                          row.executor_kind, row.pre_version_id,
                                          row.approval_id, row.approval_digest)
    if evidence is None:
        evidence = c.StageEvidence('VC/1.0', stage, self.clock.now(),
            c.canonical_json_hash('vc-coordination/1', {
                'status': status.value, 'kind': kind.value,
                'attempt': subject.attempt_id, 'row_version': row.row_version}), None)
    key = f'{request.operation_id}:{subject.attempt_id}:{kind.value}:{status.value}'
    fact = c.OperationFact(str(uuid4()), kind, request.operation_id,
        row.row_version, key, row.binding, request, 'coordination',
        subject.holder or 'coordination',
        c.ActorContext(subject.holder or 'coordination', row.binding.workspace_id,
                       subject.executor_kind or 'service'), status, evidence,
        self.clock.now(), attempt_id=subject.attempt_id, generation=subject.generation,
        pre_version_id=subject.pre_version_id, post_version_id=post_version_id,
        approval_id=subject.approval_id, approval_digest=subject.approval_digest)
    return self._io(self.facts.append, fact)
```

`AttemptSubject` is **not** a new VC/1.0 contract type — do not add it to
`contracts.py` (M02-owned, read-only per M03§2). Declare it as a private frozen
dataclass in `service.py` alongside `CoordinationRow`'s import, or pass the seven
values as keyword arguments; the named-tuple form above is for readability. This
keeps the fix inside M03's ownership.

Then fix the one mis-attributing call site. In `reserve`, capture the rejected
attempt's own identity — which for a CAS loser is the executor that *tried*, not
the row:

```python
except OwnershipError:
    self._fact(row, operation, c.FactStatus.CONFLICTED,
               subject=_rejected_subject(executor))
    raise
```

where `_rejected_subject(executor)` yields `AttemptSubject(attempt_id=None,
generation=None, holder=executor.principal_id,
executor_kind=executor.actor_kind, pre_version_id=None, approval_id=None,
approval_digest=None)`. `attempt_id=None` is correct and is what
`OperationFact`'s schema already permits (`attempt_id: UUIDString | None =
None`, `contracts.py:870`): **the loser never acquired an attempt**. Inventing
one would be worse than omitting it — it would imply an attempt existed in the
row's history when no CAS ever committed for it. The `event_key` for that fact is
then `{operation_id}:None:operation:conflicted`, which is unique per losing
request because `operation_id` is unique per request.

Dropping `row_version` from the key does not weaken uniqueness: `operation_id` is
per-request, `attempt_id` is per-acquisition, and `(kind, status)` separates the
lifecycle events within one attempt. Two appends genuinely needing the same
`(operation, attempt, kind, status)` tuple are by definition retries of one
logical fact, which is precisely the "query its key first" case C§2 requires.

**(d) RED test — write first.**

`test_conflict_facts_carry_the_rejected_requests_own_identity` in
`backend/tests/test_vc_coordination.py`.

Setup must make winner and loser **distinguishable**, which the diff's race test
deliberately does not: build two `RequestIdentity` values and two
`ExecutorContext` values with different `principal_id` and `execution_ref`, and
drive the barrier race from diff 1193–1213 with each thread using its own pair.
Use `durable_facts(h)` so `event_key` uniqueness is actually exercised.

Asserts:

1. exactly one thread returns a `Reservation`;
2. the winner's row has `attempt_id == winner.fence.attempt_id` and
   `holder == winner_executor.principal_id`;
3. **the loser's conflict fact names the loser**: the fact whose
   `request == loser_request` has `actor.principal_id ==
   loser_executor.principal_id`, `requester_id == loser_executor.principal_id`,
   `attempt_id != winner.fence.attempt_id`, and
   `approval_id is None and pre_version_id is None` — it inherited nothing from
   the winner's row;
4. **no key collision**: `len({f.event_key for f in all_facts}) ==
   len(all_facts)` and `lookup_request(binding, loser_request.idempotency_key)`
   returns `ambiguous is False`;
5. `fact_kind` participates in the key: append an `OPERATION(CONFLICTED)` and a
   `RECEIPT(CONFLICTED)` for the same request and attempt (drive via
   `h.service._fact` directly, or via BLOCK-5's recover path once it lands) and
   assert the two `event_key`s differ;
6. the key is stable across an unrelated CAS: append a fact, call
   `h.service.renew(reservation.fence)` (which bumps `row_version`), append the
   same logical fact again, and assert both appends produced the **same**
   `event_key` — so a retried ambiguous append can query it;
7. `not h.store.read(...).unresolved` is false — i.e. the winner still holds the
   row; the loser's fact did not disturb row state.

Expected RED symptom: assertion 3 fails with the loser's fact reporting
`actor.principal_id == winner_executor.principal_id` and
`attempt_id == winner.fence.attempt_id`; assertion 5 fails with both keys equal
to `'{operation_id}:{attempt_id}:{row_version}:conflicted'`; assertion 6 fails
with two keys differing only in the `row_version` segment.

**(e) Ordering.** **First.** Every other fix appends or reads a durable fact and
asserts on its identity; landing BLOCK-7 later means rewriting each of those
assertions. Concretely, BLOCK-1's `_reject` and BLOCK-5's dual append both
require the `fact_kind`-bearing key to exist, or they poison the idempotency key
they were written to free.

---

### BLOCK-8 — `reserve` treats a terminal *failure* receipt as a completed request, so a failed operation returns `ExistingReceipt` and can never be retried

**(a) Location.** `reserve`, `service.py:147–154` (diff 233–240):

```python
history = self._history(row, operation)
receipts = [f for f in history.facts if f.fact_kind == c.FactKind.RECEIPT]
if receipts:
    identities = {(f.operation_id, f.status, f.post_version_id) for f in receipts}
    if len(identities) != 1:
        raise CoordinationError('Ambiguous completed receipts')
    self._release(row)  # This reservation never acquired write authority.
    raise ExistingReceipt(receipts[0])
```

`finish` writes that receipt at `service.py:308–310` (diff 394–396) with
`c.FactStatus(result.status.value)` — so the receipt's status is whatever
terminal status M04 reported, including `FAILED` and `CONFLICTED`. The scan
filters on `fact_kind` only; status is used solely to test *agreement between
receipts*, never to test *what kind of terminal outcome* they record.

**(b) Why this violates VC/1.0.**

`ExistingReceipt` carries the docstring "Request already completed; return
original durable receipt" (`service.py:24`, diff 110), and the caller contract is
C§5.2's "same key/same completed digest → **existing receipt, no mutation**".
That clause is about a request that *succeeded*: replaying it would duplicate a
completed effect, so the correct answer is the original receipt and no new
mutation.

A `FAILED` receipt is the opposite situation. Nothing was applied. The request
*should* be retryable — that is the entire purpose of an idempotency key on a
failed command. Instead:

1. every subsequent `reserve` on that key raises `ExistingReceipt` carrying a
   `FAILED` receipt;
2. the caller, per the type's own docstring, "returns the original durable
   receipt" — reporting a *completion* for something that never happened;
3. the request can never be retried under its own key, and retrying under a new
   key defeats the idempotency guarantee (nothing links the two, so a
   double-apply becomes possible if the original in fact partially landed).

The `CONFLICTED` case is subtler and is what makes this a hard blocker rather
than a UX defect. After BLOCK-1 and BLOCK-6 land, `CONFLICTED`/`FAILED` terminal
facts become the *normal* outcome of drift and pre-send rejection. After BLOCK-5
lands, `recover` writes a `CONFLICTED` receipt to close an operation whose send
outcome was **unknown**. Read through the diff's scan, that receipt tells the
next caller "already completed" — converting an unresolved possible-send into a
fake success. That is the single most dangerous state transition in the module,
and it is why BLOCK-8 must precede BLOCK-5.

Violated clauses:

- **C§5.2** — "same key/same **completed** digest → existing receipt, no
  mutation." Scoped to completion. `failed` is not completion; C§2's status list
  enumerates `confirmed`, `noop`, `conflicted`, `failed` as distinct terminal
  statuses precisely so they are not interchangeable.
- **C§5.7** — "matching GET is observation only, **not** permission to replay."
  The mirror-image error: a conflicted receipt is not permission to *report
  success*.
- **C§2** — "`applied_unverified`/`applied_partial` end automatic mutation
  execution, **not** the unresolved-attempt safety obligation." The same
  distinction — a terminal-ish status is not a discharge of the safety
  obligation — applied to the receipt channel.
- **C§7** — the error taxonomy separates `409 conflict` from a successful
  response. `ExistingReceipt` for a conflicted request would surface to the UI as
  a completed operation; TS FM-CONCURRENCY requires "Losing request shows
  conflict, **not a success toast**."
- **TS F-IDEMPOTENCY** — "Same key same digest; ... → **Existing receipt** vs
  hard reject." The matrix's two outcomes are existing-receipt (for a completed
  request) and hard-reject (for a digest mismatch). A failed request is in
  neither bucket; it is retryable.
- **TS FM-RECOVERY** — "Quarantine/partial/unverified **distinguishable**."

**(c) Required fix.** Partition the receipt scan by success.

```python
history = self._history(row, operation)
receipts = [f for f in history.facts if f.fact_kind == c.FactKind.RECEIPT]
completed = [f for f in receipts
             if f.status in {c.FactStatus.CONFIRMED, c.FactStatus.NOOP}]
if completed:
    identities = {(f.operation_id, f.status, f.post_version_id) for f in completed}
    if len(identities) != 1:
        raise CoordinationError('Ambiguous completed receipts')
    self._release(row)  # This reservation never acquired write authority.
    raise ExistingReceipt(completed[0])
if receipts:
    # Terminal non-success: a conflict for the request, not a completion.
    self._reject(row, operation, 'Request already terminated non-successfully; file a new governed request')
```

`_reject` is BLOCK-1's helper: it verifies `_presend` (trivially true for a fresh
reservation), appends `OPERATION(CONFLICTED)` for **this** request, proves it
durable, releases the row, and raises `CoordinationError`. The caller gets a
409-class conflict with the binding intact, which is exactly C§7's mapping.

Two design points to hold.

- Do **not** relax this to "let the caller retry a `FAILED` request under the
  same key". A same-key retry would need proof that the failed attempt sent
  nothing, and M03 cannot obtain that proof for a *past* attempt whose row was
  already released — the durable evidence is gone. C§5.10's "new governed
  operation linked to the original" is the sanctioned route, so the message
  points there.
- Do **not** widen `completed` to include `CONFLICTED`. That is the BLOCK-5
  hazard above.

Note the interaction with the diff's ambiguity check: it now runs over
`completed` only. Two receipts disagreeing on `post_version_id` where one is
`CONFIRMED` and one is `FAILED` is no longer "ambiguous" — it is a confirmed
completion plus a stale failure record, and the confirmed one wins. That is the
correct reading: `finish` releases only once per attempt, so a `CONFIRMED`
receipt is proof the operation completed regardless of what earlier attempts
recorded.

**(d) RED test — write first.**

`test_terminal_non_success_receipt_is_a_conflict_not_a_completion` in
`backend/tests/test_vc_coordination.py`, parameterized
`terminal=['failed', 'conflicted']`.

Setup: `durable_facts(h)`, `enroll(h)`, `claim = admit(h)`, then complete with a
non-success terminal result —
`OperationResult(claim.request.operation_id, OperationStatus.FAILED,
claim.preimage, None, False, ('pre-send-rejected',))` for the `failed` case, and
the `CONFLICTED` equivalent — so `finish` writes a `RECEIPT` whose status is
`FAILED`/`CONFLICTED`. Then re-`reserve` with a new `operation_id` and the
**same** `idempotency_key` and `request_digest`.

Asserts:

1. the second `reserve` raises `CoordinationError` and **not**
   `ExistingReceipt` — `pytest.raises(CoordinationError)` plus an explicit
   `assert not isinstance(caught.value, ExistingReceipt)`, since `ExistingReceipt`
   subclasses `CoordinationError` and a bare `raises` would pass for the wrong
   reason;
2. a new `OPERATION`/`CONFLICTED` fact was appended for the **second** request's
   `operation_id` (BLOCK-7's attribution property);
3. the row is `IDLE`, `not unresolved`, `attempt_id is None` — `_reject`
   released it;
4. `clock.advance(61s)` then `reserve` does not produce `QUARANTINED`;
5. **positive control**: the same flow with a `CONFIRMED` result and a committed
   postimage still raises `ExistingReceipt`, and
   `caught.value.receipt.status == FactStatus.CONFIRMED` and
   `caught.value.receipt.operation_id == claim.request.operation_id` — the diff's
   `test_duplicate_completed_request_returns_receipt_without_admission` (diff
   1377–1392) must stay green;
6. `NOOP` behaves as `CONFIRMED` (C§5.3's no-op receipt is a completion):
   `ExistingReceipt` with `status == FactStatus.NOOP`;
7. mixed history: seed a `FAILED` receipt *and* a `CONFIRMED` receipt for one
   key, assert `ExistingReceipt` carrying the `CONFIRMED` one and no
   `'Ambiguous completed receipts'` error.

Expected RED symptom: assertion 1 fails because `reserve` raises
`ExistingReceipt` whose `receipt.status == FactStatus.FAILED` — the caller is
handed a "completed" receipt for an operation that applied nothing; assertion 2
fails because no new fact is appended at all; assertion 7 fails with
`CoordinationError('Ambiguous completed receipts')`.

**(e) Ordering.** After BLOCK-1 (`_reject` is defined there and is the whole
fix's mechanism) and transitively after BLOCK-7. **Before BLOCK-5** — BLOCK-5
closes a recovered operation by appending a `CONFLICTED` receipt, which is only
safe once this scan stops reading a non-success receipt as a completion.

---

### BLOCK-9 — Approved and deployed heads are written with no committed-version resolution, so an arbitrary or foreign-binding version id becomes this binding's approved head

**(a) Location.** `advance_heads`, `service.py:455–458` (diff 541–544):

```python
if update.approved is not None or update.deployed is not None:
    if (observing or not update.authorization_reference
            or not self._io(self.authorize_heads, row.binding, fence, update)):
        raise CoordinationError('Approved/deployed heads require verified policy/deployment evidence')
```

That is the **entire** validation for the two governed heads. Compare the
`observed` head at `service.py:459–469` (diff 545–555), which resolves the
version through `self.ledger.get_version`, reconstructs an `ObservationRef`, and
requires `context.binding == row.binding` plus
`self.ledger.verify_committed(ref)`. Neither `get_version` nor `verify_committed`
is ever called for `update.approved` or `update.deployed`. The values flow
straight into the CAS at `service.py:470–473` (diff 556–559).

**(b) Why this violates VC/1.0.**

`authorize_heads` cannot supply the missing check. Its signature is
`(binding, fence, update)`; it receives version *ids* and no `Version`, no
`Snapshot`, no `CaptureContext`. To decide whether `update.approved` is a version
committed to this binding it would have to query M02's ledger itself — and M03§7
forbids exactly that duplication ("Consumers mock this Protocol, never duplicate
CAS SQL"; M03§4 makes committed-evidence verification an *upstream port* M03
consumes). The callback is a policy oracle, not an evidence oracle. The diff's
own fixture proves the point: `authorize_heads=Mock(return_value=False)` flipped
to `True` (diff 1147, 1542) — a boolean with no access to lineage.

Three concrete admissions follow:

1. **A version id that does not exist.** `uid()` passes. The approved head then
   references nothing; M05's `DriftService.classify(heads, versions,
   reachability)` (C§4) cannot resolve it, and C§8's `BindingStatus.heads`
   surfaces a dangling id to the UI.
2. **A version committed to a *different binding*.** `Version.context.binding`
   is never compared to `row.binding`, so binding A's approved head can be set to
   a version of binding B. C§2's versions table carries
   `binding_id`/`binding_revision` on every row precisely so this cross-binding
   confusion is detectable; the diff declines to look.
3. **A version of an older `binding_revision`.** `BindingRef` equality includes
   `binding_revision` (C§1, "Human rebind increments `binding_revision`; all
   tokens, approvals and queries include that revision"), so a
   pre-rebind version must not become the post-rebind approved head — but with no
   comparison at all, it does.

Violated clauses:

- **C§5.8** — "Generation-conditioned head updates: observer changes observed
  only; approved requires policy authorization; **deployed requires verified
  deployment**." Verified deployment is a statement about a *committed artifact*.
  A boolean callback over an unresolved id verifies nothing.
- **C§1** — "`ObservationRef(...)` points to **already committed** evidence; an
  upload alone is insufficient." The governing principle for every version
  reference in VC/1.0: committed-ness is proven by the ledger, never asserted by
  the caller.
- **C§2 (M02 versions)** — `binding_id`, `binding_revision` NOT NULL on every
  version row, and "Exactly one row per `binding_id`" for coordination. Heads are
  the join between the two tables; an unverified join is a silent cross-binding
  leak.
- **C§4** — `DriftService.classify(heads, versions, reachability)` and
  `VersionLedger.verify_committed`. `verify_committed` exists as a port for this
  purpose and is wired into `CoordinationService.__init__` (`service.py:3`, diff
  118) — the diff has the tool and uses it for `observed` only.
- **C§8** — "Badge `In sync` derives from approved/deployed policy equality."
  Equality against an unresolvable or foreign version id is not a derivation, it
  is a coin flip. Also "Short IDs are display labels, **never** ... authoritative
  ordering" — the constraint that makes ledger resolution, not id comparison, the
  only legitimate check.
- **TS F-OPENS** — "No head regression/duplicate observation/**mislabeling**."
- **TS FM-DUAL-AUTHORITY** — "Drift reason identifies dual authority; **no
  inferred actor**." A head pointing at another binding's version manufactures
  exactly the inferred lineage this row forbids.
- **M03§3** — "One existing row per binding_id ... and three separate heads"
  under the normative DDL; the heads are M03-owned state and M03 must not persist
  unverifiable values into them.

The diff's `test_observer_cannot_regress_or_approve_heads` (diff 1506–1544)
cannot catch this: at diff 1543 it passes `final_id`, a version id that *happens*
to have been registered on `h.ledger.get_version.return_value`, and asserts only
`heads == Heads(final_id, final_id, final_id)`. Because `get_version` is a
`return_value` (not keyed by id) the test cannot distinguish "resolved and
verified" from "never looked up".

**(c) Required fix.** Resolve every governed head through the ledger and require
it to be committed to *this* binding, before the policy callback.

```python
def _committed_head(self, row, version_id, name):
    """Prove `version_id` is a version committed to this exact binding/revision."""
    version = self._io(self.ledger.get_version, row.binding, version_id)
    if version is None or version.version_id != version_id:
        raise CoordinationError(f'{name} head does not resolve to a committed version')
    if version.context.binding != row.binding:
        raise CoordinationError(f'{name} head belongs to another binding or revision')
    ref = c.ObservationRef(version.version_id, row.binding.binding_id,
                           row.binding.binding_revision,
                           version.snapshot.state_digest,
                           version.snapshot.response_envelope_digest)
    if not self._io(self.ledger.verify_committed, ref):
        raise CoordinationError(f'{name} head is not committed evidence')
    return version
```

In `advance_heads`, after BLOCK-3's authority gate and **before** the
`authorize_heads` call:

```python
for name in ('approved', 'deployed'):
    value = getattr(update, name)
    if value is not None:
        self._committed_head(row, value, name)
if update.approved is not None or update.deployed is not None:
    if (observing or not update.authorization_reference
            or not self._io(self.authorize_heads, row.binding, fence, update)):
        raise CoordinationError('Approved/deployed heads require verified policy/deployment evidence')
```

Evidence before policy is deliberate: a policy engine must never be asked to
authorize a reference the service itself cannot resolve, and putting the cheap
typed rejection first keeps `authorize_heads` off the path for malformed input
(BLOCK-3's test asserts `assert_not_called` for the authority case; this fix must
not disturb that ordering — resolution failures raise before the callback too).

`_committed_head` returns the `Version` so BLOCK-4 can reuse the resolution for
its lineage walk rather than re-reading. Note the deliberate asymmetry with the
`observed` head: `observed` additionally requires
`context.attempt_id == row.attempt_id` and
`context.generation == row.generation` (`service.py:465–466`, diff 551–552)
because an observation must have been captured *under this lease*. Approved and
deployed heads legitimately point at versions captured by earlier attempts, so
those two comparisons must **not** be copied across — doing so would make it
impossible to approve any version but the one just observed.

**(d) RED test — write first.**

`test_approved_and_deployed_heads_require_committed_versions_of_this_binding` in
`backend/tests/test_vc_coordination.py`, parameterized
`head=['approved', 'deployed']` × `defect=['unknown-id', 'foreign-binding',
'old-revision', 'uncommitted']`.

Setup note: as in BLOCK-4, `h.ledger.get_version` must be driven by a
**`side_effect` keyed by `(binding, version_id)`** returning `None` for unknown
ids — the diff's `return_value` (diff 1523, 1535) makes every id resolve to the
same `Version` and is why the gap is invisible. `h.authorize_heads.return_value =
True` throughout, so no assertion can pass merely because policy said no.

Asserts, per case:

1. `advance_heads(claim, HeadUpdate(None, <value>, None, 'ref'))` (and the
   `deployed` equivalent) raises `CoordinationError`;
2. `h.store.read(...).heads` is byte-identical to the pre-call heads and
   `h.store.compare_and_swap.call_count` did not increase — no partial write;
3. `h.authorize_heads.assert_not_called()` — evidence resolution precedes policy;
4. for `foreign-binding`, `get_version` returns a `Version` whose
   `context.binding` is `replace(h.binding, binding_id=uid())`; for
   `old-revision`, `replace(h.binding, binding_revision=1)` against a row at
   revision 2; for `uncommitted`,
   `h.ledger.verify_committed.side_effect = lambda ref: ref.version_id != value`
   so the id resolves but is not committed — each raises with a distinct message;
5. `h.ledger.get_version` was actually called with `(row.binding, value)` — the
   resolution is on the path, not skipped;
6. positive control: a version whose `context.binding == h.binding` and which
   `verify_committed` accepts **succeeds**, and `heads.approved == value`;
7. the `observed` head's stricter attempt/generation check is unchanged — the
   diff's `test_observer_cannot_regress_or_approve_heads` stays green, and a
   version captured under an *earlier* attempt is still acceptable as an
   `approved` head (proving the asymmetry in (c) was implemented).

Expected RED symptom: assertion 1 fails for every case because `advance_heads`
**returns** `Heads(<current observed>, <value>, None)`; assertion 2 then shows
the approved column populated with an id the ledger never resolved; assertion 3
fails with `authorize_heads` having been called once — proof that the policy
callback is currently the *only* gate; assertion 5 fails with
`get_version.assert_any_call` raising because the resolution never happens.

**(e) Ordering.** After BLOCK-3 — authority first: this evidence check belongs
behind the `AdmissionClaim` gate, otherwise it would be enforced for
`Reservation`-fenced callers who should never have reached the code. **Before
BLOCK-4**, whose lineage walk reuses `_committed_head`'s `get_version` +
`verify_committed` resolution.

---

### BLOCK-10 — `approval_consumption_published` and `termination_evidence` are untruthful, so a recovery worker cannot derive the safe action from durable state alone

**(a) Location.** Two dead-or-lying columns declared in
`row.py:27` and `row.py:37` (diff 67, 77) and in the DDL at
`05-coordination.sql:20` and `:29` (diff 1716, 1726).

*(i) `approval_consumption_published` is set at the wrong time and for the wrong
reason.* The only writer other than `_release` is `checkpoint`,
`service.py:434–436` (diff 520–522):

```python
self._cas(row, lambda r: r.mutation_stage == row.mutation_stage,
          mutation_stage=stage, checkpoint=checkpoint,
          approval_consumption_published=True)
```

So the column is:

| path | publishes consumption? | sets column? |
|---|---|---|
| `admit` (`service.py:253`, diff 339) | yes, `_publish_consumption(claim)` | **no** |
| `checkpoint` (`service.py:428`, diff 514) | yes | yes — **unconditionally** |
| `finish` (`service.py:307`, diff 393) | yes | **no** |
| `recover` (`service.py:355`, diff 441) | yes, when `admitted_at` is not None | **no** |
| `_release` (`service.py:283`, diff 369) | n/a | resets to `False` |

Two lies in opposite directions. After a successful `admit` the row reports
`approval_consumption_published=False` although consumption is durably published
and flush-verified. And `checkpoint` sets it `True` even when
`row.approval_id is None` — a grant-only operation with no approval to
consume — so the column asserts a consumption fact that does not and cannot
exist.

*(ii) `termination_evidence` is never written by any path.* `recover` obtains
trusted evidence at `service.py:343` (diff 429), validates it via
`_validate_termination` (`service.py:379–393`, diff 465–479), embeds it in the
`RecoveryEvidence` it appends as a fact — and never persists it to the column.
`_quarantine_row` does not write it either. `_release` clears it
(`service.py:285`, diff 371). Grep the diff: `termination_evidence` appears
exactly twice, in the row DTO and in `_release`'s reset list. The column is dead.

**(b) Why this violates VC/1.0.**

The coordination row is not a cache; C§2 specifies it as *the* mutable
coordination authority, and every column in that DDL is part of the contract a
crash-recovery reader consumes. TS "Real-platform integration tests" states the
requirement in one sentence:

> **Evidence ordering:** terminate executor after preimage commit, admission CAS,
> consumption publication and each send boundary; **a separate recovery worker
> must derive the safe action from durable state alone.**

A worker that starts after a crash between `admit`'s CAS and its
`_publish_consumption` sees `approval_consumption_published=False` — and a worker
that starts after a crash *following* a successful `admit` sees exactly the same
`False`. The two states demand different actions (publish, versus do not
re-publish and do not treat the approval as unconsumed) and the column cannot
distinguish them. It is worse than absent, because it reads as a definite "not
published".

Symmetrically, a worker inspecting a `QUARANTINED` row has no durable record of
whether trusted termination was ever obtained. C§5.9's recovery predicate is
built on that evidence; forcing every reader to re-derive it from the fact table
by scanning for `FactKind.RECOVERY` defeats the purpose of a mutable coordination
row and, in the crash-between-append-and-release window, produces a row that
looks identical to one for which no evidence exists.

Violated clauses:

- **C§5.5** — "Persist approval consumption through `OperationFacts` **before
  allowing row reuse** (prefer before PATCH). On fact-publish failure, keep claim
  unresolved, fail closed. **Old consumption survives later row reuse**; same-
  operation recovery can finish publication but does not grant a fresh mutation
  attempt." The "before allowing row reuse" gate needs a truthful row-level
  answer to "has it been published?". The diff has the column and does not
  maintain it.
- **C§5.8** — "Publish all required terminal/consumption facts and **confirm
  them** before clearing active claim. If projection/fact publication fails, keep
  unresolved; never erase the claim." Confirmation that is not recorded is not
  available to the next reader.
- **C§5.9** — "Recovery requires trusted termination of the exact prior attempt
  ... **classify against checkpoint, record recovery facts**, bump generation."
  `termination_evidence_json` is the row-level half of "record"; the DDL declares
  it for this and nothing writes it.
- **C§2 (M03 coordination)** — the DDL is normative: "One existing row per
  binding_id ... consumed approval, committed preimage, stage/checkpoint,
  unresolved/quarantine state" (M03§3 restating it). A declared NOT NULL boolean
  that does not track the fact it names is a contract violation, not cosmetics.
- **TS F-APPROVAL-REUSE** — "Historical approval after row reuse, **or crash
  before consumption publish** → Old approval cannot authorize a new operation."
  The crash-before-publish case is precisely the one the column exists to
  disambiguate.
- **TS F-CRASH-STAGES** — "lost CAS/append response; failed final fact publish →
  ... no premature release/replay."
- **TS "Fake Delta / fact store"** — "Restart fake clients/workers while
  preserving durable stores; retry through a new process identity/attempt and
  **assert unresolved old claims still block**." The assertion is about what the
  durable row says, not what the in-process object remembers.
- **M03§6** — "F-ENROLLMENT/F-IDEMPOTENCY/F-APPROVAL-REUSE/F-CRASH-STAGES
  covered with **real persisted claim recovery**."

**(c) Required fix.** Make both columns mean what they say, and only that.

Route every consumption publication through one place that records the outcome:

```python
def _publish_consumption(self, claim, row=None):
    self._io(self.facts.publish_consumption, claim)
    if not self._io(self.facts.verify_flush, claim):
        raise AuthorityUnavailable('Consumption facts not durably verified')
    # Truthful column: a durable consumption fact exists for the approval bound
    # to THIS row. No approval bound means nothing was consumed.
    if (row is not None and claim.approval_id is not None
            and not row.approval_consumption_published):
        return self._cas(row, lambda r: r.attempt_id == claim.attempt_id,
                         approval_consumption_published=True)
    return row
```

Then:

- `admit` — pass the post-CAS row: `row = self._publish_consumption(claim, row)`
  inside the existing `try`. The existing `except CoordinationError` quarantine
  (`service.py:254–258`, diff 340–344) already covers a failure of the new CAS,
  which is correct: an unverifiable column state must not leave a live claim.
- `checkpoint` — remove `approval_consumption_published=True` from the CAS at
  `service.py:434–436` (diff 520–522) and pass `row` to
  `_publish_consumption` instead. This is the change that stops the column
  asserting a consumption for approval-less operations.
- `finish` and `recover` — pass `row` likewise. In `finish` the subsequent
  `_release` resets the column, which is correct: the column describes the
  *current* claim, and `_release` clears the claim.

For termination evidence, persist it in `recover` **before** appending any fact,
so the crash window between audit append and release leaves durable proof:

```python
self._validate_termination(trusted)
row = self._cas(row, lambda r: r.attempt_id == evidence.fence.attempt_id,
                termination_evidence=c.to_wire(trusted))
```

Place it immediately after `_validate_termination` (`service.py:347`, diff 433)
and before the human/sample validation, so only *validated* evidence is ever
written. `_release` continues to clear it — the durable `FactKind.RECOVERY` fact
is the permanent record; the column is the in-flight one.

Do **not** add columns, and do **not** widen `_release`'s reset list. Both
columns already exist in the DDL and the row DTO; this is a write-path fix only,
so `05-coordination.sql` stays unchanged (as section 0's file table records).

**(d) RED test — write first.**

`test_consumption_and_termination_columns_are_truthful` in
`backend/tests/test_vc_coordination.py`, with the recovery half in
`backend/tests/test_vc_recovery.py` as
`test_recovery_persists_validated_termination_evidence_before_releasing`.

`test_consumption_and_termination_columns_are_truthful` asserts, using
`durable_facts(h)` so consumption facts are genuinely durable:

1. after `claim = admit(h)` with `h.grant.approval_id` set:
   `row.approval_consumption_published is True`, and
   `h.facts.approval_use(h.binding, h.grant.approval_id).operation_ids ==
   (claim.request.operation_id,)` — column and fact table agree;
2. **the approval-less case**: with `h.grant = replace(h.grant, approval_id=None,
   approval_digest=None)`, after `admit` **and** after
   `checkpoint(claim, CONFIG_IN_FLIGHT, ...)`,
   `row.approval_consumption_published is False` — nothing was consumed, so the
   column must not claim otherwise;
3. crash-before-publish is distinguishable: with
   `h.durable.failures.inject(CrashBoundary('before-commit'))` around `admit`,
   the resulting `QUARANTINED` row has
   `approval_consumption_published is False` **and**
   `approval_id == h.grant.approval_id` — the pair (approval bound, not
   published) is the state a recovery worker must be able to read, and it is
   distinct from case 1;
4. `checkpoint` does not flip the column on its own: after case 3's row is
   replaced by a clean `admit` + `checkpoint`, the column's value is unchanged
   from its post-`admit` value (`True` for an approval-bearing claim), and
   `h.store.compare_and_swap` was not called with
   `approval_consumption_published=True` from inside `checkpoint`;
5. `_release` resets it: after `complete(h, claim)`,
   `row.approval_consumption_published is False` and `row.approval_id is None`.

`test_recovery_persists_validated_termination_evidence_before_releasing` asserts:

6. during a successful `recover` (automatic path, `verified_request_lifetime`
   set as at diff 1618), the row carries
   `termination_evidence == c.to_wire(trusted)` at the moment the recovery fact
   is appended — capture it with a `side_effect` on `h.facts.append` that reads
   `h.store.read(h.binding.binding_id)` and stashes the row;
7. invalid termination never reaches the column: for the `wrong-attempt`,
   `missing-child` and `no-worker-proof` faults from diff 1609–1610,
   `recover` raises and `row.termination_evidence is None`;
8. a crash between the recovery append and `_release` leaves the evidence
   durable: with `h.facts.lookup_request.side_effect = OSError(...)` after the
   append, `recover` raises `AuthorityUnavailable`, the row is still
   `QUARANTINED`, and `row.termination_evidence` is populated — a second worker
   can now see that termination was already proven;
9. after a successful `recover`, `row.termination_evidence is None` (released)
   while a `FactKind.RECOVERY` fact carrying the same evidence exists.

Expected RED symptom: assertion 1 fails with
`row.approval_consumption_published is False` immediately after a successful
`admit` that provably published and flush-verified consumption; assertion 2 fails
with `True` after `checkpoint` on an approval-less claim; assertion 6 fails with
`row.termination_evidence is None`; assertion 8 fails the same way, showing the
column is never written on any path.

**(e) Ordering.** After BLOCK-7 (assertions 6–9 read appended facts and their
identity must already be correct). **Before BLOCK-5 and BLOCK-6** — both read
these columns to decide whether an attempt may be closed or released, and a fix
written against a lying column would pass for the wrong reason.

---

### BLOCK-11 — `recover` accepts the caller's `checkpoint_classification` as a free-form string and never compares it to the durable checkpoint

**(a) Location.** `recover`, `service.py:352–353` (diff 438–439):

```python
if not evidence.checkpoint_classification.strip():
    raise CoordinationError('Recovery checkpoint classification required')
```

That is the only use of the field anywhere in the diff. The durable
classification that `checkpoint` writes is at `service.py:431–433` (diff
517–519):

```python
checkpoint.update(evidence=c.to_wire(evidence),
    resume_classification=('read-only-possible-send' if stage in {
        c.PatchStage.CONFIG_IN_FLIGHT, c.PatchStage.DESCRIPTION_IN_FLIGHT}
        else 'read-only-unless-next-stage-proven-unsent'))
```

`row.checkpoint['resume_classification']` and
`evidence.checkpoint_classification` are never compared, and the recovery fact
appended at `service.py:359–360` (diff 445–446) records the caller's string as
audit truth. The diff's own tests supply `'observed-preimage'` (diff 1591) — a
value that matches neither of the two strings `checkpoint` can produce, and the
test passes.

**(b) Why this violates VC/1.0.**

C§5.9's four obligations are: trusted termination, ≥3 stable post-termination
GETs spanning the verified bound, **classify against checkpoint**, record recovery
facts and bump generation. The diff implements the first, second, fourth and
fifth. "Classify against checkpoint" is implemented as "accept any non-blank
string the caller supplies", which inverts the direction of trust: the durable
checkpoint is the authority, and the caller's assertion about it is exactly the
kind of client-supplied claim VC/1.0 refuses everywhere else (C§4:
"`AuthorizationGrant` is an internal validated value, **never a client-supplied
assertion**"; C§7: "Never accept credentials or an approver identity as
authorization").

The consequence is not merely a weak audit string. The classification is what
tells a downstream reader **which risk the recovered attempt carried**:

- `read-only-possible-send` — the attempt reached `CONFIG_IN_FLIGHT` or
  `DESCRIPTION_IN_FLIGHT`. A PATCH may be queued server-side. Any follow-up
  operation must treat live state as possibly-mutated by the dead attempt.
- `read-only-unless-next-stage-proven-unsent` — the attempt was at a
  pending/observed stage. The next stage was not sent.
- neither, i.e. `row.checkpoint` has no `resume_classification` at all — the
  attempt never checkpointed after `admit`, so `mutation_stage` is
  `CONFIG_PENDING` and nothing was sent.

An operator recovering a genuinely in-flight attempt can today record
`'observed-preimage'`, or `'nothing was sent'`, and that string becomes the
durable `FactKind.RECOVERY` evidence M06 exposes at `GET /operations/{id}`
(C§7) and M09 renders. TS FM-RECOVERY requires "Quarantine/partial/unverified
**distinguishable**" — the field that distinguishes them is caller-authored.

Violated clauses:

- **C§5.9** — "**classify against checkpoint**, record recovery facts, bump
  generation." Verbatim; the classification must be derived from the checkpoint,
  and the caller's value at most corroborated.
- **C§5.7** — "Same-operation resume is restricted to reads/evidence/finalization
  **unless durable proof shows that stage was never sent** and ownership is still
  valid." The classification *is* that durable proof, or its absence. Letting the
  caller author it lets the caller author the proof.
- **C§4** — "`RecoveryEvidence` includes ... **checkpoint classification**" as
  one field among trusted termination source/time and verified lifetime bound —
  all of which the diff validates against durable or deployment-owned state.
  Only this one is taken on faith.
- **C§2** — "Typed, schema-versioned `evidence_json` carries approvals and
  release/receipt payloads, **not unvalidated arbitrary dictionaries**. ...
  **Unknown schemas fail closed.**" An unconstrained classification string inside
  `RecoveryEvidence` is the unvalidated payload this forbids.
- **C§7** — "Recovery runs through M03/M06 authorized Job/operator contract; **UI
  displays evidence/status via operations**." The UI displays this string.
- **TS FM-RECOVERY** — "Quarantine/partial/unverified distinguishable, no
  retry-PATCH control."
- **TS "Recovery"** (integration row) — "A sandbox human recovery drill records
  actor/reason/**checkpoint classification** and residual risk; it is not
  presented as automatic safety proof." A recorded classification that the service
  did not derive is presented as safety proof while being none.
- **M03§5 task 13** — "Durable checkpoints/**resume classification**; publication
  failure retains row, no replay." The resume classification is named as durable
  state, not caller input.

**(c) Required fix.** Derive the classification from the row and reject a caller
value that disagrees.

```python
@staticmethod
def _classification(row):
    """The only authority on what a quarantined attempt may have sent."""
    recorded = (row.checkpoint or {}).get('resume_classification')
    if recorded is not None:
        return recorded
    if row.mutation_stage in {c.PatchStage.CONFIG_IN_FLIGHT,
                              c.PatchStage.DESCRIPTION_IN_FLIGHT}:
        # Stage reached in-flight without a recorded classification: fail closed.
        return 'read-only-possible-send'
    if row.admitted_at is None:
        return 'read-only-never-admitted'
    return 'read-only-presend'
```

In `recover`, replace the non-blank check at `service.py:352–353` (diff 438–439):

```python
derived = self._classification(row)
if evidence.checkpoint_classification.strip() != derived:
    raise CoordinationError(
        f'Recovery classification must equal the durable checkpoint classification ({derived})')
evidence = replace(evidence, checkpoint_classification=derived)
```

The `replace` is what makes the durable audit fact carry the *derived* value even
if the caller's string differed only in whitespace, and it sits naturally beside
the existing `replace(evidence, fence=self._fence(row))` at `service.py:358`
(diff 444). Keep the non-blank intent: an empty caller string now fails the
equality check, so the original guard is subsumed rather than removed.

Two points on the derivation.

- The in-flight-without-recorded-classification branch cannot be reached through
  `checkpoint` (which always writes one), but it *can* be reached by a row seeded
  or written by a future path, and `mutation_stage` is the DDL column a recovery
  worker reads. Failing closed to `read-only-possible-send` is C§5.7's default.
- `'read-only-never-admitted'` and `'read-only-presend'` are new strings, which is
  fine: they are M03-internal checkpoint vocabulary stored in
  `checkpoint_json`/`RecoveryEvidence`, not a `fact_kind` or `status` constrained
  by a SQL `CHECK`. Contrast BLOCK-2, where inventing a `fact_kind` *would* have
  been a contract change.

**(d) RED test — write first.**

`test_recovery_classification_is_derived_from_durable_state` in
`backend/tests/test_vc_recovery.py`, parameterized
`stage=['never-admitted', 'admitted-presend', 'config-in-flight',
'config-observed']`.

Setup per case, then `quarantine` and a full valid `recovery_evidence` with
`verified_request_lifetime` set (diff 1618):

- `never-admitted` — `reserve` only, then
  `h.service.quarantine(reservation.fence, 'worker lost')`;
- `admitted-presend` — `admit`, no `checkpoint`;
- `config-in-flight` — `admit` + `checkpoint(claim, CONFIG_IN_FLIGHT, ...)`;
- `config-observed` — plus `checkpoint(claim, CONFIG_OBSERVED, ...)`.

Asserts:

1. `recover` with `checkpoint_classification` set to the **wrong** value —
   specifically `'read-only-possible-send'` for the `admitted-presend` case and
   `'read-only-unless-next-stage-proven-unsent'` for the `config-in-flight` case,
   i.e. each case asserting the *other* case's classification — raises
   `CoordinationError` matching `'classification'`;
2. the row stays `QUARANTINED` and `unresolved` after that rejection, and no
   `FactKind.RECOVERY` fact was appended;
3. `recover` with the **correct** derived value succeeds, and the appended
   `FactKind.RECOVERY` fact's
   `evidence['checkpoint_classification']` equals the expected string per case:
   `'read-only-never-admitted'`, `'read-only-presend'`,
   `'read-only-possible-send'`, `'read-only-unless-next-stage-proven-unsent'`;
4. the diff's free-form value is now rejected: `'observed-preimage'` (the string
   at diff 1591) raises for **every** case;
5. whitespace-only and empty strings still raise (the subsumed original guard);
6. the durable fact records the *derived* string even when the caller supplies the
   same value with surrounding whitespace — proving the `replace` is on the path;
7. `read-only-possible-send` cannot be downgraded by a caller: for the
   `config-in-flight` case, no caller string produces a recovery fact claiming
   the attempt was pre-send.

Amend the two existing recovery tests accordingly: `recovery_evidence` (diff
1583–1592) must take the expected classification as a parameter, since
`test_recovery_needs_exact_attempt_termination_and_three_post_termination_reads`
and `test_unknown_request_lifetime_disables_auto_clear` both reach `recover`
through an `admit`+`quarantine` path whose derived value is
`'read-only-presend'`. That amendment is in scope per section 0 and must be
justified against C§5.9 in the handoff record.

Expected RED symptom: assertion 1 fails because `recover` **succeeds** for both
mismatched cases and returns `RecoveryResult(..., unresolved=False)`; assertion 3
fails because the appended fact's `checkpoint_classification` is whatever the test
passed in rather than a derived value; assertion 4 fails because
`'observed-preimage'` is accepted for every stage, which is the current behaviour
the diff's own test encodes.

**(e) Ordering.** After BLOCK-5 — BLOCK-5 changes what `recover` appends (audit
plus terminal receipt), and this fix changes the *content* of that audit; landing
it first means rewriting assertion 3's fact lookup once BLOCK-5 splits the
appends. Transitively after BLOCK-8, BLOCK-1 and BLOCK-7. Last in the recovery
chain.

---

### BLOCK-12 — `checkpoint`'s stage machine is not scoped to the admitted operation, and the CAS predicate does not fence the attempt

**(a) Location.** `checkpoint`, `service.py:409–436` (diff 495–522). Two coupled
defects.

*(i) The stage index arithmetic is global over `PatchStage`, not scoped to what
the admitted operation actually does.* `service.py:413–417` (diff 499–503):

```python
stages = list(c.PatchStage)
if (evidence.stage != stage or evidence.recorded_at > self.clock.now()
        or evidence.recorded_at < row.admitted_at
        or stages.index(stage) != stages.index(row.mutation_stage) + 1):
    raise CoordinationError('Checkpoint stage must advance exactly once, never replay or skip')
```

`list(PatchStage)` is `[CONFIG_PENDING, CONFIG_IN_FLIGHT, CONFIG_OBSERVED,
DESCRIPTION_PENDING, DESCRIPTION_IN_FLIGHT, DESCRIPTION_OBSERVED]`. The rule
forces **every** admitted operation through all six stages in that exact order.
The transition `CONFIG_OBSERVED → DESCRIPTION_PENDING` is index 2 → 3, so it is
*mandatory*: a config-only operation cannot terminate at `CONFIG_OBSERVED`
without either skipping checkpoints entirely or walking the description stages it
never performs. Conversely a description-only operation — the `metadata`
fingerprint changing alone, which C§1 explicitly models ("Metadata includes every
governed updateable field (description in v1)") — cannot start at
`DESCRIPTION_PENDING`, because index 3 ≠ index 0 + 1.

The diff's `test_checkpoint_normal_sequence_and_flush_failure_retains_claim`
(diff 1492–1503) walks `list(c.PatchStage)[1:]` — all five transitions — and so
encodes the global machine as if it were the contract.

*(ii) The CAS predicate fences the stage, not the attempt.* `service.py:434–436`
(diff 520–522):

```python
self._cas(row, lambda r: r.mutation_stage == row.mutation_stage,
          mutation_stage=stage, checkpoint=checkpoint,
          approval_consumption_published=True)
```

Every other CAS in the service predicates on `r.attempt_id == ...` — `_release`
(`service.py:277`, diff 363), `renew` (`service.py:175`, diff 261),
`_quarantine_row` (`service.py:325`, diff 411), `advance_heads`
(`service.py:473`, diff 559). `checkpoint` predicates on `mutation_stage`
equality alone. `_cas` passes `row.generation` and `row.row_version` as the CAS
preconditions, and `_owned` already verified the fence, so the window is narrow —
but it is the *only* stage-advancing write in the module and the one place where
"the row is still at the stage I read" is not the same question as "the row is
still *my* attempt". Belt-and-braces is the established idiom here and its
absence is a real divergence, not a style point: `_cas`'s read-back
(`service.py:95–100`, diff 181–186) checks `observed.attempt_id !=
changes.get('attempt_id', row.attempt_id)`, i.e. it compares against the
*pre-read* row's attempt, so a row whose attempt changed between the read and the
CAS is caught only because `row_version` also moved. Making the predicate
explicit removes the dependence on that coincidence.

**(b) Why this violates VC/1.0.**

- **C§5.6** — "Before **each** PATCH persist stage intent/checkpoint and reassert
  exact fence. ... **Default: no field-independent drift completion.** An
  explicit approved policy **may permit description-only completion** but final
  status remains conflicted." Two things follow. "Before each PATCH" scopes the
  stage sequence to the PATCHes the operation actually issues — not to a fixed
  six. And description-only completion is explicitly a permitted, policy-gated
  shape, which the global index arithmetic makes unreachable.
- **C§5.6** — "**reassert exact fence**". The predicate is where that reassertion
  becomes atomic with the write. Predicating on `mutation_stage` reasserts the
  stage, not the fence.
- **C§5.3** — "Persist no-op receipt when all three fingerprints match; no
  write-authorizing admission necessary, but authorization, idempotency and
  guarded finalization still apply." A no-op operation issues zero PATCHes and
  must reach `finish` from `CONFIG_PENDING`. The stage machine as written does not
  forbid that (nothing requires a checkpoint), but the six-stage march means any
  operation that checkpoints at all is committed to all six — so the
  config-only and no-op shapes are governed by two different, mutually
  inconsistent rules.
- **C§1** — `Fingerprints(config, benchmark, metadata, canonicalizer_version)`
  with metadata carrying the description. Config and metadata are separately
  fingerprinted precisely because they change independently.
- **TS F-DESCRIPTION** — "Second PATCH timeout, 5xx **or wrong metadata
  preimage** → No second-PATCH replay; partial/unverified and quarantine
  preserved." The failure matrix models config and description as separable
  PATCHes with separable failures.
- **TS "Fake Genie service"** — "Queue outcomes independently: ... **applied
  config/failed description**"; and the ordered trace `... patch_config,
  get_checkpoint, patch_description, get_post ...`. The fake is built to exercise
  a config-only or config-then-description shape, not a mandatory six.
- **M03§5 task 13** — "Durable checkpoints/resume classification; publication
  failure retains row, no replay."

**(c) Required fix.** Scope the machine to the admitted operation and fence the
attempt.

The set of stages an operation will traverse is a property of the admitted
request, so record it at `admit` time in the checkpoint that `admit` already
writes (`service.py:247`, diff 333):

```python
checkpoint={'schema_version': 'VC/1.0', 'preimage': c.to_wire(preimage),
            'stages': [s.value for s in self._planned_stages(authorization)]},
```

where `_planned_stages` derives the sequence from the grant's rendered target —
config-only, description-only, or both — and always begins at `CONFIG_PENDING`
(the state `admit`'s CAS sets, so it is the machine's zero element regardless of
shape). M03 must not *decide* which fields change; that is M04's rendering. So
derive it from the authorization the service already validated, and fail closed
if it cannot be derived:

```python
@staticmethod
def _planned_stages(authorization):
    planned = getattr(authorization, 'planned_stages', None)
    if not planned:
        # No declared plan: the conservative default is the full config+description march.
        return tuple(c.PatchStage)
    return tuple(planned)
```

`AuthorizationGrant` is M02-owned and has no `planned_stages` field today, so the
`getattr` default is what keeps this fix inside M03's ownership: **do not add the
field to `contracts.py`**. The default preserves the diff's current behaviour for
every existing caller while making the machine data-driven, and the follow-up to
give `AuthorizationGrant` a declared plan is filed as **NB-5**.

Then replace the index arithmetic:

```python
planned = [c.PatchStage(v) for v in (row.checkpoint or {}).get('stages')
           or [s.value for s in c.PatchStage]]
if row.mutation_stage not in planned or stage not in planned:
    raise CoordinationError('Stage is not part of this admitted operation')
if planned.index(stage) != planned.index(row.mutation_stage) + 1:
    raise CoordinationError('Checkpoint stage must advance exactly once, never replay or skip')
```

and fence the write:

```python
self._cas(row, lambda r: (r.attempt_id == claim.attempt_id
                          and r.mutation_stage == row.mutation_stage),
          mutation_stage=stage, checkpoint=checkpoint)
```

Note `approval_consumption_published=True` is gone from this CAS — that removal
belongs to BLOCK-10 and is why BLOCK-10 lands first; here the line is simply not
reintroduced.

Keep `evidence.recorded_at < row.admitted_at` and
`evidence.recorded_at > self.clock.now()` unchanged, and keep them as a *separate*
raise from the sequencing check so the two failure reasons stay distinguishable
in the message — the diff conflates four conditions into one message, filed as
**NB-4**.

**(d) RED test — write first.**

`test_checkpoint_stage_machine_is_scoped_to_the_admitted_operation` in
`backend/tests/test_vc_coordination.py`, parameterized
`shape=['config-only', 'description-only', 'config-and-description']`, driving the
shape through the `planned_stages` the fixture attaches to `h.grant` (a
`SimpleNamespace`-style attribute on the frozen grant is not possible, so use
`dataclasses.replace` on a test-local subclass, or set
`h.grant = replace(h.grant)` and monkeypatch `_planned_stages` — the test asserts
on behaviour, not on how the plan is injected).

Asserts:

1. `config-only`: `checkpoint(claim, CONFIG_IN_FLIGHT, ...)` then
   `checkpoint(claim, CONFIG_OBSERVED, ...)` both succeed, and
   `complete(h, claim)` **succeeds** from `CONFIG_OBSERVED` — a config-only
   operation terminates without walking the description stages;
2. `config-only`: `checkpoint(claim, DESCRIPTION_PENDING, ...)` raises
   `CoordinationError` matching `'not part of this admitted operation'`, and the
   row's `mutation_stage` is unchanged at `CONFIG_OBSERVED`;
3. `description-only`: the first checkpoint is
   `DESCRIPTION_PENDING` and it **succeeds** from `CONFIG_PENDING`;
4. `description-only`: `checkpoint(claim, CONFIG_IN_FLIGHT, ...)` raises;
5. every shape: replaying the current stage raises, and skipping one planned
   stage raises — the monotonicity property the diff already has must survive;
6. **attempt fencing**: with the row's `mutation_stage` held constant, mutate
   `h.store.state.rows[:]` to a row with a different `attempt_id` (the technique
   the diff uses at diff 1247) and assert `checkpoint` raises **and** the row is
   unchanged — the CAS predicate rejected a foreign attempt rather than relying on
   `row_version` having moved;
7. `evidence.recorded_at` before `row.admitted_at` and after `clock.now()` each
   raise with a message distinguishable from the sequencing message;
8. the diff's `test_checkpoint_normal_sequence_and_flush_failure_retains_claim`
   (diff 1492–1503) stays green under the default plan — the `getattr` fallback
   preserves the six-stage march when no plan is declared.

Expected RED symptom: assertion 1 fails at `complete(h, claim)` — no, more
precisely, assertion 1's *checkpoints* succeed and `complete` succeeds even
today, so the load-bearing red is assertion 2, which fails because
`checkpoint(claim, DESCRIPTION_PENDING, ...)` **succeeds** (index 3 = index 2 + 1)
on a config-only operation; assertion 3 fails with
`CoordinationError('Checkpoint stage must advance exactly once, never replay or
skip')` because index 3 ≠ index 0 + 1, so a description-only operation cannot
checkpoint at all; assertion 6 fails because the CAS commits under a foreign
`attempt_id` whenever `row_version` and `generation` happen to match.

**(e) Ordering.** After BLOCK-6 — `_presend` reads `row.mutation_stage` and
`row.checkpoint`, and BLOCK-6 establishes what a proven pre-send row is; scoping
the stage machine changes which rows qualify, so the release semantics must be
settled first. After BLOCK-10, whose fix removes
`approval_consumption_published=True` from the very CAS this finding rewrites —
landing BLOCK-12 first would reintroduce it. Transitively after BLOCK-7. Last on
its branch.

---

## 4. Non-blocking findings

None of these blocks the merge. Each is real, each is cheap, and three of them
(**NB-3**, **NB-4**, **NB-5**) are forward-referenced from section 3 because the
blocking fix rewrites the same line — do those *with* their block, not
separately. The rest may be batched into one cleanup commit after all twelve
blocks are green, and none of them needs a RED test of its own unless noted.

**NB-1 — `ExistingReceipt` subclasses `CoordinationError`, so a success signal is
caught by every failure handler.** `service.py:23` (diff 109). The docstring is
honest about the motive — "Non-admitting result channel; preserves the frozen
`reserve()` return type" — and the frozen signature in C§4 does force a
side channel. But the consequence is that `except CoordinationError` (the
idiomatic fail-closed handler this module teaches) silently swallows a
*completed request* and reports it as a coordination failure, and
`pytest.raises(CoordinationError)` passes for the wrong reason — the diff's own
`test_crashes_between_evidence_cas_consumption_patch_and_release_remain_safe`
(diff 1451) would not notice an `ExistingReceipt` where it expects a fault.
Cheapest correct fix without touching C§4: keep the subclassing but add an
explicit `is_failure = False` class attribute (`True` on `CoordinationError`), and
have M04's handlers test it. BLOCK-8(d) already requires the stricter
`assert not isinstance(caught.value, ExistingReceipt)` idiom in new tests; adopt
it repository-wide for M03 while here. A proper fix — `reserve` returning
`Reservation | ExistingReceipt` — is a VC/1.0 signature change and out of M03's
scope (M03§3, "frozen in `contracts.md` §4").

**NB-2 — the loser's conflict-fact append can mask the `OwnershipError` it is
recording.** `reserve`, `service.py:144–146` (diff 230–232):
`self._fact(row, operation, c.FactStatus.CONFLICTED)` runs inside
`except OwnershipError:` and reaches `_io`, so an append failure raises
`AuthorityUnavailable` *instead of* the original `OwnershipError`. The caller then
sees a 503 where C§7 specifies a 409 ("`409 conflict/key mismatch/drift/ambiguous
binding`" vs "`503 coordination/evidence unavailable`"), and — worse for
diagnosis — the record of *why* the attempt lost is gone from both the exception
and the fact table. Wrap the append and re-raise the original:
`try: self._fact(...)` / `except CoordinationError: pass` is **not** acceptable
(it would silently drop the FM-CONCURRENCY conflict fact M03§6 requires); instead
chain it — `raise` the original with the append failure as `__cause__`, or raise a
combined `AuthorityUnavailable` whose message names both. Either way the 409/503
distinction must survive. Worth a small dedicated test given TS FM-CONCURRENCY's
frontend assertion.

**NB-3 — `x or y` cannot express "clear a head".** `advance_heads`,
`service.py:470–472` (diff 556–558): `update.observed or row.heads.observed` and
the two siblings treat every falsy value as absent. Harmless today because heads
are UUID strings or `None`, but the idiom is wrong for a nullable field and the
three lines are being rewritten by BLOCK-4 anyway. Replace with explicit
`is None` tests **inside BLOCK-4's commit**; BLOCK-4(c) already specifies this.

**NB-4 — `checkpoint` collapses four distinct rejections into one message.**
`service.py:413–417` (diff 499–503) raises `'Checkpoint stage must advance exactly
once, never replay or skip'` for: `evidence.stage != stage` (caller passed
mismatched arguments), `recorded_at` in the future (clock skew or forged
evidence), `recorded_at < admitted_at` (evidence predates the admission), and the
actual sequencing violation. Only the last matches the message. TS FM-RECOVERY
requires "Quarantine/partial/unverified **distinguishable**", and an operator
reading `GET /operations/{id}` cannot distinguish these four. Split into three
raises with accurate messages **inside BLOCK-12's commit**, which rewrites this
block; BLOCK-12(c) and (d)(7) already specify the split and its assertion.

**NB-5 — `AuthorizationGrant` carries no declared stage plan, so BLOCK-12's fix
has to default conservatively.** BLOCK-12(c) derives the planned stage sequence
via `getattr(authorization, 'planned_stages', None)` with a full six-stage
fallback, because `AuthorizationGrant` (`contracts.py:594`) is M02-owned and
adding a field is a VC/1.0 change M03 may not make (M03§2 lists M02 shared DTOs as
read-only; C§preamble: "Breaking changes require a version bump and consumer test
updates"). The `getattr` is deliberately ugly so it reads as a seam, not a
design. Follow-up, **owned by M02 with M04 as the other consumer**: add
`planned_stages: tuple[PatchStage, ...]` to `AuthorizationGrant`, populated by
M06's `authorize` from the rendered target diff, and drop the `getattr`. Until
then a config-only operation is *permitted* to walk the description stages it
never sends — which is the pre-BLOCK-12 behaviour, so nothing regresses; it is
merely not yet tight. File against M02; do not implement inside M03.

**NB-6 — the 60-second lease duration is hard-coded in three places.**
`service.py:141` (diff 227, `reserve`), `service.py:176` (diff 262, `renew`) and
`service.py:448` (diff 534, `observe_exclusively`) each write
`self.clock.now() + timedelta(seconds=60)`. Every test that exercises expiry
depends on the literal via `clock.advance(timedelta(seconds=61))` (diff 1567, and
BLOCK-2/BLOCK-6's new tests). M03§8 lists "Genie maximum server request lifetime
is unresolved" as a deployment-time measurement, and the lease is the knob a
deployment will want to turn against that measurement. Promote it to a
constructor keyword — `lease_duration: timedelta = timedelta(seconds=60)` — beside
`verified_request_lifetime`, which is already injected for exactly this reason
(`service.py:37–39`, diff 123, 139). Note the observation lease and the mutation
lease may eventually want different values (TS F-OPENS names "slow GET"
explicitly), so a single knob is the minimal step, not the final shape.

**NB-7 — `transition_sequence` is `row.row_version`, which does not order
transitions within one row version.** `_fact`, `service.py:118` (diff 204).
C§2's `CONSTRAINT operation_sequence CHECK (transition_sequence >= 0)` is
satisfied, so this is legal, but the column's purpose is to order an operation's
transitions and `row_version` only advances on a *CAS*. Any two facts appended
between two CASes share a sequence — which BLOCK-5's audit+receipt pair does by
construction, and BLOCK-1's `_reject` may do alongside an existing conflict fact.
Ordering then falls back to `recorded_at`, which the explicit test clock makes
identical within a call. Low impact because `event_key` (after BLOCK-7) is unique
and readers can order by kind, but if M06 ever projects a transition timeline it
will render these facts in arbitrary order. Cheapest fix: derive the sequence as
`row.row_version * 8 + <ordinal within the CAS window>`, or better, leave the
column and file the semantics question against M06, which owns the projection.
Do **not** change it during BLOCK-5 — a key change and a sequence change in one
commit makes the RED symptom ambiguous.

**NB-8 — the integration gate accepts `QUARANTINED` as evidence of a conflict.**
`assert_conflict`, diff 960–965:
`assert history.facts[0].status in {c.FactStatus.CONFLICTED,
c.FactStatus.QUARANTINED}`. For the CAS-loser assertion in
`test_real_delta_same_row_cas_one_winner` the expected status is `CONFLICTED` and
nothing else — a loser that somehow quarantined the binding would pass this
assertion while representing a far worse outcome than the one being tested. The
union is only needed by
`test_real_delta_expired_lease_quarantines_without_takeover` (diff 1074), which
calls the same helper for a genuinely quarantined row. Parameterize the expected
status per call site rather than unioning them. This is the one NB that weakens a
*release gate* rather than production code, so do it in the same cleanup commit
rather than deferring — TS "Never describe skipped integration tests as passed"
is the surrounding principle and a too-permissive assertion is the same failure in
a quieter form.

**NB-9 — `_cas`'s read-back repeats three comparisons the generic loop already
covers.** `service.py:95–100` (diff 181–186): the explicit `holder`, `attempt_id`
and `generation` checks use `changes.get(key, row.<key>)`, so **when the key is
present in `changes` they are subsumed** by the following
`any(getattr(observed, key) != value for key, value in changes.items())`. When the
key is *absent* they are not redundant — they assert the field was left unchanged,
which is genuinely valuable and is why this is a readability note rather than a
deletion. Make the intent explicit: keep one loop over `changes` and one loop over
the three identity fields *not* in `changes`, with a comment saying so. Do not
"simplify" by deleting the explicit checks — section 1 names this read-back as the
diff's most important correct behaviour (C§5.1, C§5.4) and
`test_two_concurrent_reservations_have_exactly_one_winner`'s lying-adapter case
(diff 1227–1233) depends on it.
