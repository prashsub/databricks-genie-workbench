# M04 B1 real fix — add M03 public pre-send reject seam (cross-module)

## Why this exists
The M04 fix re-review (claude_code) proved B1 is STILL unresolved. The prior B1 fix routes a pre-send reviewed-base conflict to `coordination.finish()` with a synthesized AdmissionClaim built from the RESERVED reservation fence. But real M03 `finish()` calls `assert_owner()` which requires `row.state == ADMITTED`; a pre-send conflict row is still `RESERVED` (admit was deliberately never called), so `finish()` raises `OwnershipError`. Net: the binding is still BRICKED and the caller now gets an internal OwnershipError instead of a clean CONFLICTED. After the 60s lease, `_expire()` quarantines the stuck RESERVED row anyway.

The reviewer also proved the B1 unit test is misleading: it uses `Mock(spec=vc.Coordination)` whose `finish_presend` fake sets `occupied=False` itself — the test implements the release it's meant to verify, so it passes without the property holding against real M03.

Root cause: **M03 has no PUBLIC seam to release a proven pre-send RESERVED row.** `_reject()` (which publishes the terminal CONFLICTED fact then `_release()`s, guarded by `_presend(row)`) is private, reached only from inside `reserve()`/`finish()`/`_history()`. The gate holds only a `Reservation` and cannot reach it.

## The fix (cross-module, integrator-coordinated; VC/1.0 contract evolution authorized by the integrator)
Add a new PUBLIC coordination method and wire the gate to it.

### Change 1 — CONTRACT (backend/services/version_control/contracts.py, the `Coordination` Protocol ~line 957)
Add a new Protocol method, synchronous, alongside `finish`:
```
    def reject_reservation(self, reservation: Reservation, message: str) -> None: ...
```
Semantics (document in a docstring/comment): definite pre-send rejection of a RESERVED (never-admitted) attempt — publishes the terminal CONFLICTED fact and releases the binding to IDLE. Fails closed (raises) if the row is not a proven pre-send RESERVED row owned by this reservation (i.e. it must NOT be usable to bypass the possible-send quarantine obligation). This is the counterpart to `finish` for the pre-admission conflict path.

Update the golden/enum or protocol-shape tests if test_vc_contracts.py asserts the exact Coordination method set (it parses contracts.md / the Protocol) — keep it in sync so that test stays green.

### Change 2 — M03 IMPL (backend/services/version_control/coordination/service.py)
Implement `reject_reservation(self, reservation, message)`:
- Resolve the owned row via `self._owned(reservation.fence)` (validates revision/attempt/generation/lease, not stale, not quarantined).
- Require `row.state == c.CoordinationState.RESERVED` (never-admitted) — else raise OwnershipError ("Only a reserved pre-send attempt can be reject-released"). This prevents using it to skip the possible-send quarantine on an ADMITTED/in-flight row.
- Validate the reservation identity matches the row: `reservation.request == self._request(row)` and executor principal/ref match `row.holder`/`row.executor_ref` (mirror the checks in `admit`).
- Then delegate to the existing private path: `self._reject(row, reservation.request, message)` — which already asserts `_presend(row)`, publishes the CONFLICTED fact, verifies it durably, `_release()`s, and raises CoordinationError(message). (Because `_reject` raises CoordinationError by design, decide the public contract: EITHER let reject_reservation swallow that final CoordinationError and return None after a successful release, OR re-raise. RECOMMENDED: return None on successful release — the gate wants a clean release, not an exception. Keep the AuthorityUnavailable raise if the fact isn't durably verified — that must still fail closed.)
- Net post-condition on success: row is IDLE, unresolved=False, a durable CONFLICTED fact exists, heads/observer sequence preserved.

### Change 3 — M04 GATE (backend/services/version_control/mutation_gate.py, the pre-send conflict branch ~line 30-35)
Replace the synthesized-AdmissionClaim + `_finish(CONFLICTED)` approach with:
- Still record the terminal CONFLICTED durable fact for the operation IF the gate is the fact author here — BUT note M03's `_reject` already publishes the CONFLICTED coordination fact. Avoid double-publishing/conflicting facts: decide the single source of truth. Given M03's `_reject` publishes the conflict fact and releases atomically, the gate should call `self.coordination.reject_reservation(reservation, "Reviewed base changed before admission")` and then return an `OperationResult(..., CONFLICTED, preimage, None, unresolved=False, evidence_references=...)`. If the gate ALSO needs an OperationFacts entry (M06 facts vs M03 coordination facts are different stores), keep the gate's own `facts.append(CONFLICTED)` (that was the correct part of B2) but do NOT call coordination.quarantine and do NOT call finish. Ensure exactly-once semantics and no double coordination fact.
- The preimage/external state must be preserved in the returned result.

### Change 4 — M04 TEST (backend/tests/test_vc_mutation_gate.py)
Rewrite `test_pre_send_base_conflict_releases_binding_without_quarantine` so the "binding not bricked" property is verified where it lives:
- Drive the gate against the REAL `CoordinationService` (import from backend.services.version_control.coordination) wired with the M02 fakes (append-only fact store, single-row CAS coordination double is NOT enough here — use the real CoordinationService with the fake stores it accepts), NOT a `Mock(spec=vc.Coordination)` whose fake performs the release.
- Assert: result.status == CONFLICTED; no PATCH sent; external preimage preserved; a durable CONFLICTED fact exists; the coordination row is back to IDLE/unresolved=False; AND a subsequent real `reserve()` for a corrected request SUCCEEDS (the actual not-bricked proof).
- RED evidence: reverting reject_reservation to the old `finish(synthesized claim)` (or to quarantine) must make this test FAIL against the real service.
- If other M04 unit tests use Mock(spec=vc.Coordination) for unrelated paths that's fine; only the B1 test must use the real service to prove the release.

## Owned paths (integrator-sanctioned cross-module for this fix)
- backend/services/version_control/contracts.py (M02) — add the Protocol method + keep test_vc_contracts.py green.
- backend/services/version_control/coordination/service.py (M03) — implement reject_reservation.
- backend/tests/test_vc_coordination.py (M03) — add a RED test for reject_reservation: a RESERVED pre-send row reject-releases to IDLE with a durable CONFLICTED fact; an ADMITTED/in-flight row is REFUSED (must not bypass quarantine); a foreign reservation is refused.
- backend/services/version_control/mutation_gate.py (M04) — call the new seam.
- backend/tests/test_vc_mutation_gate.py (M04) — rewrite the B1 test against real CoordinationService.
- Also update test_vc_contracts.py if it pins the Coordination method set.
Do NOT touch anything else.

## TDD + gates
- STRICT TDD: write the M03 reject_reservation RED test and the rewritten M04 B1 test FIRST (both fail against current code / a reverted seam), then implement.
- Run from worktree ROOT:
  - `python -m pytest backend/tests/test_vc_coordination.py backend/tests/test_vc_recovery.py backend/tests/test_vc_mutation_gate.py backend/tests/test_vc_contracts.py -q` green
  - full offline `python -m pytest backend/tests -q --ignore=backend/tests/integration -p no:cacheprovider` green (was 962; must stay green)
- Confirm write switches default-off (test_vc_composition.py) and integration collection unchanged (14).

## Commits
One commit for the contract+M03 seam (+its test), one for the M04 gate+test rewrite. Incremental commits mandatory (Astra rate-limits).
