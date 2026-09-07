# xmod-fix follow-up: correct the execute() retry short-circuit set (cross-review BLOCK)

Integrator-sanctioned fix, continuing in worktree /home/sandbox-agent/workspace/worktrees/xmod-fix
(branch vc/xmod-actor-retry-fix, currently at HEAD 0188353). Run pytest with the repo venv:
/home/sandbox-agent/workspace/databricks-genie-workbench/.venv/bin/python -m pytest ... (from this worktree root).

STRICT TDD: write each RED test FIRST, confirm meaningful failure, implement minimum, commit after each fix. Do NOT weaken any existing safety assertion or guard.

## Background
`mutation_gate.execute()` (backend/services/version_control/mutation_gate.py ~line 22) short-circuits to `verify_only()` when a prior fact indicates a prior attempt. The current code:
```
execution_outcomes = {APPLIED_UNVERIFIED, APPLIED_PARTIAL, CONFIRMED, CONFLICTED}
if any(fact.fact_kind == vc.FactKind.OPERATION and fact.status in execution_outcomes for fact in history.facts):
    return self.verify_only(...)
```
`history = facts.lookup_request(...)` returns EVERY `FactKind.OPERATION` fact for the idempotency key, including facts written by `coordination/service.py`, not just facts written by this gate's `_record`/`_finish`. The set was derived from the wrong source. Cross-review (independent, mutation-tested end-to-end) found:

- **1a: `CONFLICTED` is a PRE-SEND status on its producers.** `coordination/service.py` writes `FactKind.OPERATION`+`CONFLICTED` on a mere `reserve()` CAS-loss (`_rejected_subject`, `attempt_id=None`) and in `_reject()` when `_presend(row)` is true (nothing was ever PATCHed). The gate's OWN pre-send producer (`_record` on the reviewed-base-changed path, ~line 41, uses `reservation.fence`, pre-admission) also writes CONFLICTED pre-send. VERIFIED: after transient contention leaves one `('operation','conflicted')` fact, retrying the SAME approved request returns `applied_unverified`/`unresolved=True` with `patch_config_once` NEVER called — an approved governed write is permanently wedged and mis-reported as possibly-applied.
- **1b: `NOOP` is omitted but is a COMPLETED terminal outcome.** `mutation_gate.py` `_finish(..., OperationStatus.NOOP, ...)` persists `OPERATION`/`NOOP`. The established precedent set at `governance/approvals.py:102-103` treats `{CONFIRMED, NOOP, COMPENSATED, FAILED}` as "completed request cannot authorize a new mutation." VERIFIED: with a prior `OPERATION/NOOP`, the gate proceeds to reserve, and real coordination `reserve()` raises `ExistingReceipt` (a bare CoordinationError/RuntimeError) that NO production caller handles (`restore.py:29`, `jobs/vc_reconcile.py:39`) — a completed no-op becomes an unhandled exception instead of `verify_only`.

## The correct invariant
Short-circuit to `verify_only` IFF a prior OPERATION fact for this key represents EITHER:
  (a) a possibly-sent attempt (a PATCH may have been issued — must not re-PATCH), OR
  (b) a completed terminal outcome (operation already finished — re-running is wrong).
Otherwise (a pre-send-only history: reserved/rejected/conflicted-before-send, never sent, not complete) PROCEED to first execution.

## TASKS (investigate first, then implement precisely — do NOT blindly copy a status set)

1. **INVESTIGATE the real producers.** Read `coordination/service.py` and `mutation_gate.py` fully to enumerate every place an `OPERATION` fact is written and with what status + whether a PATCH could have been sent at that point. In particular determine, for each status, whether it is reachable ONLY pre-send, ONLY post-send, or BOTH:
   - `APPLIED_UNVERIFIED`, `APPLIED_PARTIAL`: post-send (PATCH issued, outcome uncertain) → MUST short-circuit.
   - `CONFIRMED`: completed terminal → MUST short-circuit.
   - `NOOP`: completed terminal (no PATCH needed, all fingerprints matched) → MUST short-circuit.
   - `CONFLICTED`: reachable PRE-SEND (reserve CAS-loss with attempt_id=None; reviewed-base-changed before admission) — and possibly POST-SEND if your model marks conflicted after a PATCH attempt. Determine this precisely.
   - `QUARANTINED` (coordination/service.py ~455, written at the possible-send boundary): decide whether it represents a possibly-sent attempt. Today the gate relies on the coordination row staying `unresolved` (failing the retry closed via `_owned`) rather than its own filter — make the gate's OWN filter correct rather than depend on that side-effect.

2. **DISCRIMINATE pre-send vs post-send for CONFLICTED (and any dual-reachable status).** Do NOT short-circuit on a pre-send CONFLICTED (that wedges approved writes — 1a). DO short-circuit on a post-send CONFLICTED (a PATCH may have gone out — no double-PATCH). Use a reliable discriminator: `fact.attempt_id is not None` (pre-send coordination CONFLICTED has attempt_id=None) and/or the recorded PatchStage in the fact's evidence (`stage not in {CONFIG_PENDING, None}` indicates a PATCH ladder was entered). Pick the discriminator that is actually populated on these facts — verify by reading how `_record`/`_finish`/coordination write the fact's fields.

3. **ADD `NOOP`** to the completed-terminal short-circuit condition (1b).

4. **Preserve intact:** the `RuntimeError("Ambiguous or reused request identity")` guard and the same-key+different-digest rejection (`any(fact.request != request.identity ...)`), evaluated BEFORE the short-circuit. Preserve no-double-PATCH: any prior possibly-sent/terminal attempt still routes to `verify_only` with no second PATCH.

5. **Consider a shared constant.** Three sites now encode "completed/possibly-sent" membership (`approvals.py:102`, `coordination/service.py:186`, and this gate) with different memberships. If clean, define the canonical set/predicate ONCE (e.g. a module-level constant or helper in `contracts.py`) and reference it — but only if it does not force touching files outside the owned-paths below. If it would, keep it local with a clear comment and note the drift risk as a backlog item in your report.

## Required RED tests (in backend/tests/test_vc_mutation_gate.py) — write FIRST, confirm they fail against current HEAD
- `test_presend_cas_loss_conflicted_does_not_block_first_execution`: seed ONE `FactKind.OPERATION`/`CONFLICTED` fact with `attempt_id=None` (and pre-send PatchStage e.g. CONFIG_PENDING/None); assert the gate proceeds — `reserve`/`admit` called and `patch_config_once` (or the config PATCH) called once. RED today: returns applied_unverified with zero PATCHes.
- `test_completed_noop_history_routes_to_verify_only`: add `FactStatus.NOOP` to the existing parametrized allow-list test (~line 138) OR a dedicated test; assert it routes to `verify_only` (no reserve). RED today: proceeds to reserve.
- `test_postsend_conflicted_still_routes_to_verify_only` (NO-DOUBLE-PATCH regression guard): seed an `OPERATION`/`CONFLICTED` fact that represents a POST-send attempt (attempt_id set, PatchStage past config-pending); assert it short-circuits to `verify_only` and does NOT re-PATCH. This proves your discriminator does not reintroduce a double-PATCH.

## Owned-paths (unchanged from xmod-fix)
ONLY: backend/services/version_control/mutation_gate.py, backend/tests/test_vc_mutation_gate.py. (If a shared constant genuinely requires contracts.py, STOP and report instead — do not touch it without confirmation, as contracts.py is the frozen VC/1.0 seam.) Do NOT touch coordination/service.py, identity.py (already fixed), routers, main.py, auth.py, or other modules.

## FINAL
Run `python -m pytest --ignore=backend/tests/integration -q` from repo-root context — green, ZERO regressions. Report: which statuses you determined pre-send vs post-send vs terminal (with the code evidence), the exact discriminator used for CONFLICTED, RED->GREEN evidence per test, final offline pass count, owned-paths confirmation, and any shared-constant/backlog note.
