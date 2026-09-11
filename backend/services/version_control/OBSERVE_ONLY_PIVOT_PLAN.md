# Observe-Only Optimizer Pivot — Execution Plan

**Status:** In progress (Step 1 complete) · **Branch:** `vc/observe-pivot` (off `vc/unblock`)
**Supersedes:** `packages/genie-space-optimizer/M10_IMPLEMENTATION.md` (M10 governed-optimizer path — to be removed)
**Target merge:** `feature/version-control-ci-cd`, based off `origin/feature/metric-view-advisor`

## 1. Decision

Stop *governing* the optimizer. Make the optimizer's code **pristine** (identical to
`feature/metric-view-advisor`), let it run its own self-healing flow, and have Version
Control **observe** it (snapshot before/after) for history + restore. **Keep**
cross-workspace promotion (M07 / D2.6) and the shared governance core it depends on.

Rationale: the optimizer is already self-safe — non-improving iterations are rolled back
(`unified_loop.py` calls `rollback(...)` at the current-config snapshot). The invasive
candidate-sandbox / champion-gate model (M10) required editing the optimizer's internals,
which is out of this feature's purview. An observe-only model keeps the optimizer untouched
and is a fraction of the remaining work, while promotion (the actual dev→prod safety
boundary) stays fully governed.

## 2. Principles / constraints

- **Do not touch optimizer surface.** End state: `packages/genie-space-optimizer/src` is
  byte-identical to `origin/feature/metric-view-advisor`.
- **No regression** to promotion (M07 / D2.6 stays green) or to optimizer behavior on
  `metric-view-advisor`.
- The optimizer is trusted (self-rollback); VC records but does not gate it.
  **Accepted tradeoff:** a crashed run can leave a space mid-changed — VC captures the
  result but does not prevent it. Governance is enforced at the *promotion* boundary.

## 3. Target architecture

- **Optimizer:** runs in dev, writes directly, unmodified. The MV-advisor `/trigger`
  features (`operator_guidance`, join-advice seeds, MV attach hook) return for free with
  the revert to base.
- **VC observe:** capture pre-run base + post-run result as immutable ledger versions
  (`origin=optimizer`, `optimizer_run_id` stamped) → history + restore.
- **Promotion:** unchanged — governed, approved, atomic, receipted (dev→prod).

## 4. Scope — REMOVE (optimizer-governance / M10 slice only)

- Delete `backend/services/version_control/optimizer_adapter.py` (isolated — nothing
  outside the optimizer and its own tests imports it).
- Delete `packages/genie-space-optimizer/src/genie_space_optimizer/integration/version_control.py`
  (candidate/champion model).
- Revert to base: the 5 guard files (`optimization/applier.py`, `optimization/unified_loop.py`,
  `optimization/preflight.py`, `optimization/space_quality_enrichment.py`,
  `common/genie_client.py`), the 2 job blockers (`jobs/run_optimize.py`,
  `jobs/run_benchmark_qc_and_repair.py`), and the 3 gutted integration files
  (`integration/apply.py`, `integration/discard.py`, `integration/revert.py`).
- Revert to base: `backend/routers/auto_optimize.py` + `backend/routers/create.py`
  (restore functional endpoints).
- Surgically remove the `optimizer_apply` handler/seam/kind-switch in
  `backend/jobs/__init__.py`, the `optimizer` field in `GovernedSeams`, and any
  optimizer-only type in `contracts.py`. (`platform/live_seams.py` already passes
  `optimizer=None`, so the live graph never used it — low risk.)
- Delete optimizer VC tests (candidate isolation/lifecycle, champion apply, revert guard,
  `tests/integration/test_vc_optimizer_platform.py`); revert the optimizer unit tests that
  were rewritten for candidate clients; revert `test_auto_optimize_router.py` /
  `test_create_agent.py` fail-close assertions to base.
- Delete `packages/genie-space-optimizer/M10_IMPLEMENTATION.md`.

## 5. Scope — KEEP (shared; promotion needs it)

`governance/`, `coordination/`, `mutation_gate.py`, `ledger.py`, `registry.py`,
`canonical_diff.py`, `contracts.py` (shared types), `observer.py`, `drift/`, `restore.py`,
all `promotion/`, `platform/` (minus the optimizer seam), `backend/jobs` (minus the
optimizer handler), `vc_mutations.py`, and the promotion scripts + **D2.6 fixtures**.

## 6. Scope — ADD (observe-only) — larger than "reuse observer.py"

The observe path is **not currently wired into the running app**:

- `append_observation` is gated: requires `writes_enabled=True` **and** the version +
  observer identity to belong to the **trusted target workspace** (`ledger.py`).
- `backend/main.py` composes `app.state.version_control` but does **not** mount the
  `vc_mutations` observe/restore router, and `spaces.py` history is the *scan* history
  (`get_score_history`), **not** the VC ledger. Nothing captures today.

So the add is a real (bounded) integration:

1. Compose a live observer (ledger + transport + coordination + identity + status_reader)
   as the **trusted target-workspace SP**, with the versions-table write path enabled.
2. **Capture-before** in the restored `/trigger` (records the base) and **capture-after**
   (see §11 open decision), stamping `origin=optimizer` + `optimizer_run_id`.
3. Expose a **history / restore read surface** (mount a read endpoint or extend `spaces`
   history) if the UI should show VC versions.
4. Gate behind `vc_history_enabled` (+ the ledger write flag) in the target config.

## 7. Non-goals (explicitly out of scope)

No M10 / candidate sandbox / governed optimizer apply. No change to promotion semantics.
No optimizer code changes beyond reverting to base. Not enabling any Genie-space *mutation*
governance for the optimizer.

## 8. Risks & regression areas + mitigations

- **R1 Dangling refs** after deleting the candidate model/adapter → grep must return zero
  for `assert_candidate_write` / `require_candidate_session_for_job` / `optimizer_adapter` /
  `integration.version_control`; offline tests + typecheck.
- **R2 Dual-touched files** (`applier`, `unified_loop`, `genie_client`, `run_optimize`):
  vc/unblock's edits there are purely guards, so `checkout base` is clean and loses no
  non-VC work. Confirm per-file diff before committing.
- **R3 Job-graph surgery** breaking promotion/restore → `live_seams` already `optimizer=None`;
  `test_vc_runtime_assembly` / `test_vc_composition` / `test_vc_governed_ports` must stay green.
- **R4 Test reconciliation** — fork fail-close tests will fail once endpoints are functional;
  revert them to base and delete candidate/champion tests.
- **R5 Observe wiring (highest)** — capture is a gated write with a trusted-identity
  requirement and isn't wired today; treat it as the main build item, not a snippet.
  Decide capture-after mechanism + identity + flag.
- **R6 Merge (positive)** — reverting optimizer files to base eliminates the
  `auto_optimize.py` + `unified_loop.py` merge conflicts; likely only `pyproject.toml`
  pytest markers remain.
- **R7 create-agent** — reverting restores functional create; confirm create-agent is
  intentionally *not* governed (recommended: yes — it creates a new space, not a
  managed-binding mutation).
- **R8 MV-advisor tests** — the base's `operator_guidance` 200-tests must pass against the
  restored functional router.
- **R9 Baseline reset** — record new green counts for backend (`./scripts/test.sh`) and
  GSO (`uv run pytest`).

## 9. Validation (prove no regression)

- `git diff origin/feature/metric-view-advisor -- packages/genie-space-optimizer/src` →
  **empty** (optimizer truly pristine).
- Grep-clean for all candidate/adapter symbols.
- Backend + GSO offline suites green; promotion tests (`test_vc_promotion*`,
  `test_vc_runtime_assembly`) green.
- Lint + typecheck on touched files.
- Merge dry-run into `feature/version-control-ci-cd` (off `metric-view-advisor`) → expect
  ≤1 trivial conflict.
- Live (nonprod): one optimizer run writes exactly a before + after version; one promotion
  smoke (D2.6 already validated).

## 10. Sequence

1. Work on a fresh `vc/observe-pivot` branch. **(done)**
2. Revert the optimizer slice (§4) → offline green + grep-clean.
3. De-wire the optimizer seam (jobs/seams/contracts) → promotion tests green.
4. Reconcile tests → full offline baseline green.
5. Build observe capture (§6) → offline tests for capture.
6. Live nonprod validation of capture (+ promotion smoke).
7. Seamless merge into `feature/version-control-ci-cd`.

## 11. Open decisions

- **Capture-after mechanism:** `capture_on_open` (passive, simplest) vs. poll the job
  `run_id` to completion (tight bracket) vs. both. *(Gates Step 5.)*
- Does the UI need a VC version-history / restore tab now, or is silent capture-for-restore
  enough?
- `create-agent` stays functional / ungoverned? (recommended: yes.)
- Flag + identity for capture (`vc_history_enabled` + ledger writes; app SP as trusted
  target identity).

## 12. Rough effort

Revert + de-wire + test reconcile + merge: ~2–3 days. Observe-capture wiring (composition +
hooks + flag + identity, optional history UI + live check): ~3–6 days. Total ≈ 1–1.5 weeks —
vs. ~3–4 weeks to finish M10.

## 13. Mechanism refinement (discovered during Step 2)

**Finding:** the fork branch (`vc/unblock` @ `8c3da097`) diverged from base at merge-base
`ae8c4367` and **never took base's 83 commits** — it lacks base's entire MV-advisor feature
(13 base-only backend files: `mv_create.py`, `mv_entitlement.py`, `mv_suggest.py`,
`join_advisor.py`, their tests; plus the optimizer-package MV modules `mv_advisor.py`,
`mv_attach.py`, …). Therefore a **file-by-file revert of the optimizer slice on the fork
branch** does not just "undo guards" — reverting `auto_optimize.py` to base pulls in base's
whole MV feature by hand (import cascade). That is fragile and effectively re-builds base's
branch manually.

**Conflict surface is tiny.** Only **10 files are dual-touched** (changed by both base and
fork since `ae8c4367`). Non-test dual-touched files:

| File | Resolution |
|---|---|
| `backend/routers/auto_optimize.py` | take **base** (functional trigger + MV features); re-add observe-before hook in Step 5 |
| `packages/.../optimization/unified_loop.py` | take **base** |
| `packages/.../optimization/applier.py` | take **base** |
| `packages/.../common/genie_client.py` | take **base** |
| `packages/.../jobs/run_optimize.py` | take **base** |
| `pyproject.toml` | combine (markers + addopts; both small/additive) |
| `databricks.yml` | combine (base +24 / fork +40, additive) |
| `scripts/deploy_lib/gso_job.py` | combine (base +13 / fork +3, additive) |
| `frontend/src/lib/api.ts` | combine (base +282 MV UI / fork +17 VC, disjoint sections) |

Everything else is a clean one-sided add: base's MV feature comes in automatically, and the
fork's VC-keep files (`backend/services/version_control/**`, `vc_*` routers/jobs) add cleanly.

**Refined mechanism (supersedes §10 steps 2–4 & 7):** construct the target by **merging fork
into a base-derived `feature/version-control-ci-cd`** with the resolution policy above,
instead of reverting file-by-file on the fork branch. This is why the earlier merge felt
non-seamless — it had no resolution policy for the optimizer `/trigger`; we now do
("take base for optimizer-governance; combine for VC-shared; add VC-keep").

**Refined sequence:**
1. `vc/observe-pivot` off `vc/unblock` holds this plan doc. **(done)**
2. Create `feature/version-control-ci-cd` off `origin/feature/metric-view-advisor`.
3. `git merge` the fork (`vc/unblock`) into it; resolve the 10 conflicts per the table.
4. **Post-merge cleanup** = the rest of §4's REMOVE that isn't covered by conflict
   resolution (these are fork-only adds the merge keeps): delete
   `backend/services/version_control/optimizer_adapter.py`,
   `packages/.../integration/version_control.py`, the GSO candidate/champion tests,
   `backend/tests/test_vc_optimizer_adapter.py`, `M10_IMPLEMENTATION.md`; de-wire the
   optimizer seam in `backend/jobs/__init__.py`; revert to base any fork-only GSO/router
   tests written for the candidate model (`test_auto_optimize_router.py`,
   `test_create_agent.py`, GSO `conftest.py` candidate fixture + rewritten unit tests).
5. Grep-clean + full offline green (backend + GSO).
6. Build observe capture (§6); live nonprod validation.
7. Branch **is** the target — open PR / land.

**Validation the merge is correct:** after resolution + cleanup,
`git diff origin/feature/metric-view-advisor -- packages/genie-space-optimizer/src` is
empty, and grep for `assert_candidate_write` / `require_candidate_session_for_job` /
`optimizer_adapter` / `integration.version_control` is zero.

## 14. Progress log

- **Steps 1–5 complete.** `feature/version-control-ci-cd` created off base and the fork
  merged in (`815c27e3`, 2-parent: base `923a78be` + fork `b9732a0d`). 226 files differ
  from base = the net VC surface.
- Resolutions applied per §13 table: optimizer package == base (empty diff verified);
  `auto_optimize.py`/`create.py` == base; `pyproject.toml` combined. Deleted
  `optimizer_adapter.py`, GSO `integration/version_control.py`, candidate/champion GSO
  tests, `M10_IMPLEMENTATION.md`, `test_vc_optimizer_adapter.py`. De-wired the optimizer
  seam (`jobs/__init__.py`, `live_seams.py`, `GovernedSeams.optimizer`,
  `contracts.OptimizerChampionAdapter` + its spec entry in
  `docs/design/.../contracts.md`). Reverted `test_auto_optimize_router.py` /
  `test_create_agent.py` to base; adjusted `test_vc_runtime_assembly.py` /
  `test_vc_governed_ports.py` for the removed optimizer handler.
- **Decision taken (minimal de-wire):** the inert `optimizer_apply` kind + flag +
  capability vocabulary is retained (reserved/unwired/fail-closed), matching the
  codebase's existing pattern for other not-yet-wired kinds. Full excision of that
  vocabulary is a possible follow-up.
- **Offline baseline:** `./scripts/test.sh` → **3237 passed**. Remaining 3 failed + 2
  errored are base's MV-advisor doc-parity GSO tests (`test_gap_report_counts`,
  `test_rules_parity`, `test_exposure_matrix`) that read **gitignored** local files
  (`docs/design/mv-advisor-{gap-report,playbook}.md`) absent in this clone — identical on
  base, not a regression.
- **Not yet done:** frontend build/lint check (`api.ts` auto-merged), Step 6 observe
  capture (blocked on §11 capture-after decision), Step 7 live nonprod validation + PR.
- **Open cleanup (optional):** the M10 design docs
  (`docs/design/version_control/implementation_plan/module-10-optimizer-integration.md`,
  `testing-strategy.md` optimizer_adapter refs) still describe the removed governed-
  optimizer model — decide whether to prune/annotate them.
