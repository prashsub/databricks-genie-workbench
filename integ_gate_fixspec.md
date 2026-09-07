# Integration-Gate Reconciliation Fixspec (M06 ↔ M08 seam)

## Context
When M06 (Approvals/Audit) is integrated onto the trunk that already contains M08
(Provisioning/Identity), the full contract regression suite goes RED on ONE M08 test:
`backend/tests/test_vc_provisioning.py::test_integration_gate_command_collects_every_integration_test`.

Two invariants in that M08 gate test conflict with M06's current layout:
1. **Layout invariant (KEEP):** no `@pytest.mark.integration` decorator may appear in any
   `backend/tests/*.py` OUTSIDE `backend/tests/integration/`. M06 currently has 5 integration
   tests declared INLINE in `backend/tests/test_vc_audit.py` (decorators at lines ~94, 108, 122, 134;
   5 collected functions). This violates the layout invariant.
2. **Count invariant (BRITTLE — must be made robust):** the gate asserts
   `len(collected) == 5` (exactly five integration tests total under backend/tests/integration/).
   M08's own 5 baseline tests satisfy it today, but the moment M06 adds 5 more the total becomes 10
   and the `== 5` assertion fails. This exact-count hardcode will break on EVERY future module that
   adds integration tests (M03, M04, M05, M07, M10). It must become count-agnostic while PRESERVING
   the real anti-silent-skip property.

Trunk is currently pristine at a68c62a (M06 was NOT pushed — broken state was correctly not integrated).

## Owned paths for THIS fix (integrator-sanctioned cross-module reconciliation)
- `backend/tests/test_vc_audit.py`               (M06 — remove the 5 inline integration tests)
- `backend/tests/integration/test_vc_audit_live.py`  (M06 — NEW file, the relocated 5 tests)
- `backend/tests/test_vc_provisioning.py`        (M08 — make the gate count-agnostic)
Touch ONLY these three. Do NOT touch any other module's files, main.py, auth.py, or conftest.py.

## TASK A — Relocate M06's integration tests (M06 side)
1. Identify the 5 integration-marked test functions in `backend/tests/test_vc_audit.py`
   (the ones decorated `@pytest.mark.integration`: real Delta append-only/durable-replay,
   real target-SP membership revocation + human identity, real Delta+evidence-Volume outage
   disables authorization, plus the others in that block — enumerate them precisely from the file).
2. MOVE them verbatim (marker, docstrings/honest-blocker comments, the `platform` fixture usage,
   and all needed imports) into a NEW file `backend/tests/integration/test_vc_audit_live.py`,
   following the exact convention M08 already uses in
   `backend/tests/integration/test_vc_permissions.py` / `test_vc_provisioning_live.py`
   (read those two files first to match import style, the `platform`/conftest fixture wiring, and
   the honest deployment-blocker docstring pattern). These remain honest deployment blockers —
   they MUST stay `@pytest.mark.integration` so they are deselected offline and only collected by
   the gated command. NEVER claim they pass.
3. REMOVE those 5 functions (and their now-unused imports) from `backend/tests/test_vc_audit.py`.
   The remaining offline tests in test_vc_audit.py must still pass.

## TASK B — Make M08's gate count-agnostic (M08 side), TDD-first
The gate test currently (around lines 380-402 of backend/tests/test_vc_provisioning.py):
```
result = subprocess.run([... "backend/tests/integration", "-m", "integration", "--collect-only", "-q"] ...)
assert result.returncode != 5, ...          # KEEP (collected something, not silent-skip)
assert result.returncode == 0, ...           # KEEP (no collection error)
collected = re.findall(r"^(backend/tests/integration/\S+::\S+)$", result.stdout, re.MULTILINE)
assert len(collected) == 5, ...              # <-- BRITTLE: replace
marker_decorator = re.compile(r"^\s*@pytest\.mark\.integration\b", re.MULTILINE)   # KEEP layout scan
for path in (ROOT/"backend"/"tests").glob("*.py"): assert not marker_decorator.search(...)  # KEEP
```
Replace the brittle `assert len(collected) == 5` with a **count-agnostic** but equally-strong
anti-silent-skip check: count how many `@pytest.mark.integration`-decorated test functions are
actually DECLARED across `backend/tests/integration/*.py` (scan those files for the decorator), and
assert `len(collected) == <declared integration count>` AND `len(collected) >= 5` (baseline never
regresses). This directly enforces "every declared integration test is collected — none silently
skipped" without pinning a magic total, so it survives every future module adding integration tests.

STRICT TDD: FIRST write/adjust the RED evidence — with M06's 5 tests relocated (Task A done first),
the OLD `== 5` assertion would see 10 collected and FAIL; demonstrate that RED, then implement the
count-agnostic assertion so it passes at 10, and confirm it would still catch a genuine silent-skip
(e.g., a mis-marked/mis-placed integration test). Keep the KEEP-marked assertions intact.

## Ordering
Do Task A first (relocate), commit it, then Task B (gate robustness), commit it. Commit after each.

## Verification (run from worktree ROOT, not backend/)
- `python -m pytest backend/tests/test_vc_audit.py -q`  → offline audit tests green (no integration collected offline)
- `python -m pytest backend/tests/integration -m integration --collect-only -q`  → collects 10 (M08's 5 + M06's 5)
- `python -m pytest backend/tests/test_vc_provisioning.py -q`  → the gate test now GREEN with 10 integration tests
- `python -m pytest backend/tests --ignore=backend/tests/integration -q`  → full offline suite green, zero regressions
Report exact numbers, RED→GREEN evidence for Task B, and confirm the 5 relocated tests remain honest
`@pytest.mark.integration` deployment blockers (never claimed passing).
