# Integration-gate reconciliation #2 — parser must recognize module-level `pytestmark`

## Context
Integrating M03 onto the trunk (which already has M08's integration-gate test) fails:
```
test_integration_gate_command_collects_every_integration_test:
  AssertionError: Expected all 10 declared integration cases collected, saw 14
test_integration_gate_rejects_partial_collection: (cascades from the same parser gap)
```

## Root cause (confirmed)
The gate's AST parser in `backend/tests/test_vc_provisioning.py` (function `test_integration_gate_command_collects_every_integration_test`, the `for path in (ROOT/backend/tests/integration).glob("*.py")` loop, ~lines 393-412) counts a test function as an integration case ONLY if it carries a **per-function** decorator `@pytest.mark.integration`.

M06 (`test_vc_audit_live.py`) and M08 (`test_vc_permissions.py`, `test_vc_provisioning_live.py`) use per-function `@pytest.mark.integration` → counted (10 cases).

M03 (`test_vc_delta_cas.py`) uses **module-level** `pytestmark = pytest.mark.integration` (a valid, idiomatic pytest convention) with NO per-function decorators → its 4 tests are collected by pytest but counted as 0 by the parser. Hence declared=10, collected=14.

Both conventions are valid pytest. Future modules (M04/M05/M07/M10) may use either. The gate must be robust to both, not force one convention.

## Required fix (STRICT TDD, TEST-ONLY, M08-owned file)
Extend the AST parser so that, for each file in `backend/tests/integration/*.py`, it detects a **module-level integration mark** and, when present, counts every test function in that module as an integration case (still multiplying by each function's own `@pytest.mark.parametrize` literal cases).

Detection of a module-level mark: a module-level assignment to the name `pytestmark` whose value references `pytest.mark.integration` — handle both:
- `pytestmark = pytest.mark.integration`
- `pytestmark = [pytest.mark.integration, ...]` (list/tuple containing it)

Counting rule per integration file:
- If the module has a module-level integration mark: every `def test_*` / `async def test_*` function in the module is an integration case (case_count still multiplied by that function's parametrize literal-list/tuple lengths). A per-function `@pytest.mark.integration` on top is redundant but must NOT double-count.
- Else (no module-level mark): keep existing behavior — count only functions carrying the per-function `@pytest.mark.integration` decorator (× parametrize).
- Net: `declared_count` must equal the real pytest-collected count. After M03 lands, declared == collected == 14.

Also update the **layout-pin scan** (the `marker_decorator` regex that forbids `@pytest.mark.integration` OUTSIDE `backend/tests/integration/`): a module-level `pytestmark = pytest.mark.integration` in a NON-integration unit-test file must ALSO be forbidden (else a module could smuggle integration marks into a unit file via pytestmark and dodge the layout invariant). Extend the scan to catch a module-level `pytestmark` referencing `pytest.mark.integration` in any `backend/tests/*.py` outside the integration dir. Keep the existing per-function-decorator scan too.

Keep the `>= 10` cumulative floor and the `test_integration_gate_rejects_partial_collection` behavior intact (it should still detect a deselected case).

## TDD requirement
1. RED FIRST: add/adjust a focused test (or a scratch fixture directory) proving the parser now counts a module-level-`pytestmark` file's tests. A clean way: create a tiny temp integration file under a tmp dir in the test using module-level `pytestmark` and assert the parser counts its functions — OR rely on the real state: with M03's `test_vc_delta_cas.py` (module-level pytestmark, 4 tests) present, the OLD parser yields declared=10≠collected=14 (RED), the NEW parser yields declared=14==collected=14 (GREEN). Capture the RED (old parser) evidence.
2. GREEN: `python -m pytest backend/tests/test_vc_provisioning.py -q` passes, and the full offline suite passes.
3. Do NOT weaken any existing assertion (silent-skip detection, partial-collection rejection, layout pin, conftest fail-not-skip, cross-dir import ban). Only ADD module-level-pytestmark handling.

## Owned paths (STRICT)
- ONLY `backend/tests/test_vc_provisioning.py` (the gate test).
- Do NOT edit M03 files, service code, DDL, or any other module. M03's use of module-level pytestmark is legitimate and stays as-is.

## How to reproduce the integrated state for testing
This fix must be proven with M03 present. Work in worktree `worktrees/m06-integ` is NOT right (no M03). Instead: the dispatcher will run you in a dedicated worktree that has BOTH M08's gate and M03's integration test. Verify from the worktree ROOT:
- `python -m pytest backend/tests/test_vc_provisioning.py -q` → green
- integration collection: `python -m pytest backend/tests/integration -m integration --collect-only -q` → 14 cases

## Commit
Single commit, message naming the module-level-pytestmark gate reconciliation. Incremental commit after GREEN.
