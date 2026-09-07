# Cross-module fix: actor_kind vocabulary drift (BLOCKER 1) + M04 requested-only retry short-circuit (BLOCKER 2)

These are integrator-sanctioned fixes to ALREADY-INTEGRATED shared modules (M08 identity, M04 gate, and possibly M06). They were surfaced by M07 wiring M08's real identity into M04/M06's real gates end-to-end for the first time. Each module's own tests pass in isolation because each assumed its own value; only the end-to-end path exposes the drift.

Worktree: /home/sandbox-agent/workspace/worktrees/xmod-fix (branch vc/xmod-actor-retry-fix, based on trunk 28c471b which has M01-M06,M08).
Repo venv: /home/sandbox-agent/workspace/databricks-genie-workbench/.venv/bin/python (run pytest from repo ROOT).

STRICT TDD for each fix: write/adjust the RED test FIRST, confirm meaningful failure, then implement minimum to pass. Commit after each fix. Do NOT weaken any existing safety assertion.

---
## BLOCKER 1 (CONFIRMED) — actor_kind vocabulary drift

`backend/services/version_control/platform/identity.py` (M08) produces:
- line ~62: `actor_kind = "user"` (OBO/PAT human-on-behalf path)
- line ~68: `actor_kind = "service_principal"` (OAuth M2M profile / Job service-principal path)

But every governed-write / approval gate in the integrated codebase uses a DIFFERENT vocabulary:
- Governed writes require `actor_kind == "service"`:
  - `backend/services/version_control/mutation_gate.py:148` (`executor.actor_kind != "service"`)
  - `backend/services/version_control/governance/approvals.py:187` (target-side executor `!= 'service'`)
  - `backend/services/version_control/governance/facts.py:167` (`grant.actor.actor_kind == 'service'`)
  - `backend/services/version_control/restore.py:26` (`!= "service"`)
- Human approval requires `actor_kind == "human"`:
  - `backend/services/version_control/governance/approvals.py:153` and `:259` (`actor.actor_kind != 'human'`)

CONSEQUENCE: an M08-minted Job executor (`service_principal`) is REJECTED by M04/M06/restore governed-write gates → the ENTIRE governed write path (restore, promotion, optimizer champion apply, prod edit) can never pass with a real identity. And an M08-minted OBO human (`user`) would be rejected by M06 human-approval checks (`human`).

RESOLUTION: the canonical vocabulary is the one used by the 5 already-integrated-and-reviewed gate files: **`"service"` and `"human"`**. M08's identity provider is the outlier and must be corrected.

TASKS:
1. FIRST verify by test: in `backend/tests/test_vc_identity.py`, the service-principal path assertion (find the test for the OAuth M2M / profile executor) and the human/OBO path (line ~166 currently asserts `actor_kind == "user"`). Change the EXPECTED values to `"service"` (M2M/Job path) and `"human"` (OBO path) FIRST so the tests go RED against the current wrong implementation. If no explicit service-path assertion exists, ADD one: `assert provider.executor(<m2m selection>).actor_kind == "service"`.
2. Then fix `identity.py`: line ~62 `actor_kind = "user"` -> `"human"`; line ~68 `actor_kind = "service_principal"` -> `"service"`.
3. Grep the WHOLE repo for any other producer/consumer of `"service_principal"` or the human `"user"` actor_kind literal (tests, fixtures, golden JSON in tests/fixtures/vc_*). Reconcile every one to the `service`/`human` vocabulary. Do NOT leave any file on the old vocabulary. (Do NOT confuse this with unrelated `user_name`/`service_principal_name` fields on Databricks SDK run_as objects — those are SDK attribute names, not actor_kind values; leave them.)
4. Re-run the affected module suites from repo root: `python -m pytest backend/tests/test_vc_identity.py backend/tests/test_vc_mutation_gate.py backend/tests/test_vc_composition.py backend/tests/test_vc_approvals.py backend/tests/test_vc_operation_facts.py backend/tests/test_vc_audit.py backend/tests/test_vc_restore.py -q` — all green.

---
## BLOCKER 2 (NEEDS CONFIRMATION, then fix if real) — M04 requested-only retry short-circuit

`backend/services/version_control/mutation_gate.py` `execute()` (lines ~23-28):
```
history = self.facts.lookup_request(request.binding, request.identity.idempotency_key)
if history.ambiguous or any(fact.request != request.identity for fact in history.facts):
    raise RuntimeError("Ambiguous or reused request identity")
if history.facts:
    return self.verify_only(request.identity.operation_id, executor)
```

CLAIM (from M07 implementer): M04 treats ANY existing request history as a completed retry — so if a governed operation was only REQUESTED (an approval request written by M06 with the same idempotency_key, but the mutation not yet admitted/applied), `history.facts` is non-empty and `execute()` short-circuits to `verify_only(...)` and returns `applied_unverified` WITHOUT ever performing admission + PATCH. i.e. a first real execution of an approved-but-not-yet-applied request never runs.

CONFIRM FIRST (write a failing test in `backend/tests/test_vc_mutation_gate.py`):
- Construct the realistic scenario: a governed request whose idempotency_key already has a prior fact that is NOT a terminal applied/completed outcome (e.g. a REQUESTED / approval-consumption fact), then call `execute()`.
- Assert the CORRECT behavior: the gate proceeds to reserve -> GET -> admission CAS -> fenced PATCH ladder (i.e. it does NOT short-circuit to verify_only for a non-terminal/requested-only history). A retry short-circuit must only trigger when there is a prior fact representing an ACTUAL prior EXECUTION attempt of the SAME operation (applied/unverified/partial/completed terminal outcome), NOT a mere requested/approval fact.
- If the test does NOT fail against current code (i.e. lookup_request already filters to execution facts and requested-only facts don't appear), then BLOCKER 2 is NOT real — record that finding clearly and make NO code change. Verify what `facts.lookup_request` actually returns (does it return approval/requested facts or only mutation-execution facts?) by reading M06 facts + M02 contracts before concluding.

FIX (only if confirmed real): make the retry short-circuit condition precise — only treat history as a completed prior attempt when a prior fact records an actual execution outcome for this operation_id (applied_unverified / applied_partial / completed / conflicted terminal), not a requested/approval-only fact. Preserve the existing idempotency and no-double-PATCH invariants: the same idempotency_key + same request identity that ALREADY has a real prior PATCH attempt must still route to verify_only (no second PATCH); only a requested-but-never-executed history should proceed to first execution. Keep the ambiguous/reused-identity RuntimeError guard intact.

Do NOT relax the "same key + different request digest = rejection" rule (the `any(fact.request != request.identity)` guard). Do NOT introduce a blind replay path.

---
## OWNED-PATHS for this fix (integrator-sanctioned cross-module set)
- backend/services/version_control/platform/identity.py + backend/tests/test_vc_identity.py
- backend/services/version_control/mutation_gate.py + backend/tests/test_vc_mutation_gate.py
- any tests/fixtures/vc_* golden JSON or test files that hardcode the old actor_kind vocabulary (reconcile only the actor_kind literal)
Do NOT touch routers, main.py, auth.py, other modules' service code, or M07's promotion/ files.

## FINAL
Run the FULL backend offline suite from repo root: `python -m pytest --ignore=backend/tests/integration -q` — must be green with ZERO regressions. Report: BLOCKER 2 confirmed-or-not (with evidence), files changed, RED->GREEN evidence per fix, final offline pass count, and confirmation nothing outside the owned-paths set changed.
