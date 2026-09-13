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

## 11. Open decisions — RESOLVED

- **Capture-after mechanism:** **BOTH** — `capture_on_open` as the always-on passive
  baseline, plus job-poll of the optimizer run id for a tight before/after bracket.
- **UI:** **Include a history/restore read surface** — mount a read endpoint + a minimal
  history/restore UI tab (the fork already ships `frontend/src/components/version-control/`).
- `create-agent` stays functional / ungoverned. **(done — reverted to base.)**
- Flag + identity for capture: `vc_history_enabled` + ledger writes enabled, app SP as the
  trusted target identity.

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

## 15. Step 6 detailed build plan (post-reconnaissance)

Both the backend and frontend VC surfaces were mapped. Step 6 is a real platform
integration, larger than §6 first implied, and its live path is **deploy-only-validated**
(no local server). Offline we build wiring + fake-based tests; live capture is Step 7.

### 15.1 What already exists (fork-built, dormant)
- **Backend routers (unmounted):** `vc_mutations` (POST observe, POST restore),
  `vc_reconcile` (GET overview, GET status, POST reconcile), `vc_operations`
  (GET operation, audit), `vc_releases`, `vc_approvals`. `main.py` composes an empty,
  all-flags-off container and mounts none of them.
- **Capture/ledger:** `Observer.capture_on_open/capture`; `ledger.history()`/`get_version()`
  are reads (no write flag); `append_observation` needs `writes_enabled` **and** binding +
  observer identity in the trusted target workspace.
- **Enrollment:** `registry.enroll(EnrollmentRequest, actor)` creates a provisional binding
  for a `space_key` (a write; needs the M03 initializer + `writes_enabled`).
  `registry.resolve(binding_id)` reads it back. **Observing a space requires a binding.**
- **Frontend (demo-mocked):** `frontend/src/components/version-control/` has real
  presentational `History` + `RestorePanel` + `VersionControlTab`, a real
  `lib/version-control-api.ts` client, but everything defaults to `demoApi` (in-memory
  mock) and is only reachable via `?view=version-control`
  (`pages/VersionControlWorkbench.tsx`). Not on SpaceDetail/AdminDashboard.

### 15.2 Backend gaps to close
1. **Live compose** (`platform/` — new builder, fail-closed until configured, mirroring
   `live_seams.build_governed_seams`): construct Delta registry + ledger + coordination +
   canonicalizer + transport + identity + status_reader + Volume artifact adapters as the
   trusted target-workspace SP, register them into the `Composition`, `writes_enabled` +
   `vc_history_enabled` from config. Build an `Observer` + restore service from these.
2. **History-list endpoint** — the frontend calls `versions()`/`diff()` which have **no
   backend route**. Add `GET /api/version-control/bindings/{id}/versions` (→ `ledger.history`)
   and optionally `/versions/{vid}` + `/diff` (→ `get_version` + canonicalizer compare).
3. **Mount routers** in `main.py`, supplying ports from `app.state.version_control`, gated on
   `vc_history_enabled` (reads) and the write flags (observe/restore/enroll).
4. **`/trigger` hooks** in `auto_optimize.py`: on trigger, resolve-or-**enroll** the space →
   **capture-before**; then **capture-after** via BOTH `capture_on_open` (passive) and a
   job-poll of the optimizer `run_id`. Stamp `optimizer_run_id` (see next).
5. **Observer run-id stamping** — `Observer.capture` sets `Origin.EXTERNAL` and no
   `optimizer_run_id`; add a capture variant that accepts `optimizer_run_id`/`champion_id`
   so after-run versions carry provenance.
6. **`request.state.vc_auth`** — the observe/restore routers read it; the OBO middleware (or
   a small dependency) must populate the authenticated VC actor.

### 15.3 Frontend changes
- Swap `demoApi` → `new VersionControlApi(fetch)` in `VersionControlWorkbench.tsx`,
  `use-version-control.ts`, `version-control-tab.tsx` (keep `demoApi` for Vitest only).
- Fix `version-control-api.ts`: operations → `/api/vc/operations/{id}`; restore body
  `expected_base` as 64-hex digest string (not `Fingerprints`); align approvals route.
- Source `binding_id` from the space's binding (via overview/status) rather than the demo
  shell; decide the mount location (see decisions).

### 15.4 Decisions (Step 6) — RESOLVED
- **UI mount: BOTH** — a per-agent "Version Control" tab in `SpaceDetail` **and** the
  top-level overview (promote `VersionControlWorkbench` to a real nav destination).
- **Binding lifecycle: AUTO-enroll on first optimizer trigger** — `/trigger` resolves-or-
  enrolls the space's binding, then captures. Enrollment writes are gated behind the VC
  write flag (no-op until enabled at deploy).
- **Live config/identity:** catalog/control-schema/warehouse for the versions table +
  Volume, and the trusted SP identity — provided at deploy (Step 7); offline stays
  fail-closed.
- **History/diff endpoints:** add the history-list endpoint now; **defer semantic `diff`**
  (drive history/detail from `ledger.history`/`get_version`).

### 15.5 Sequence (Step 6)
a. Observer run-id capture variant + offline test.
b. History-list endpoint (`GET .../versions` → `ledger.history`) + offline tests. DONE
   (`backend/routers/vc_history.py`, gated on `vc_history_enabled`). Version **detail**
   and **diff** deferred: the frontend's `VersionDetail` is a flat summary+snapshot and
   restore's `expected_base` is a digest string, so those shapes are reconciled with the
   frontend in step (f) rather than shipping a mismatched backend contract now.
c. Live-compose builder (fail-closed) + register ports + offline fake tests.
d. Mount routers + `vc_auth` population + flag gating.
e. `/trigger` enroll + capture-before/after (both) + offline tests with fakes.
f. Frontend de-mock + route/body fixes + SpaceDetail tab.
g. (Step 7) deploy to nonprod: provision versions table/Volume, set flags + SP, validate
   one optimizer run writes before+after versions and restore works.

## 16. CUJ-1 persistence: inline-Delta, size-guarded, no Volumes

**Status:** Decided (design) · **Supersedes:** the Volume-fork path in §15.1 / §15.2 /
§15.5g for the CUJ-1 (in-workspace history + restore) surface.

### 16.1 Decision

Scope the persistence design to **CUJ-1 only** (in-workspace capture / history / restore).
CUJ-2 (cross-workspace promotion) may be deprecated, so it does not get a vote in this
design. For CUJ-1 we **mimic the optimizer**: store the whole Genie config **inline in the
Delta `genie_space_versions` table** and **remove the UC Volume fork entirely**. When a
config is ever too large to write inline, fail **loudly** with an explicit error — never
silently divert to a Volume. One write path, one atomic `INSERT`, no second storage system.

Rationale: the optimizer (GSO) already proves the inline pattern at the same payload scale —
it stores full configs as plain Delta `STRING` columns (`config_snapshot`,
`config_json`, `observed_config_json` in `optimization/ddl.py`) with no Volume and no size
cap. The ledger's own row is already 95% inline: `serialized_space_json`,
`canonical_state_json`, and `restorable_metadata_json` are inline `STRING` today
(`ledger.py:167-173`). Only the raw `response_envelope` is subject to the 32 KB cap
(`ledger.py:82` `inline_max_bytes=32768`) and forks to `/Volumes/.../vc_snapshots/`
(`ledger.py:126-138`). That fork is the sole reason CUJ-1 needs a Volume — and it is the
exact step that failed in nonprod (see §16.6).

### 16.2 Column type — keep `STRING`, do not switch to `VARIANT`

All payload columns are `STRING` today (`01-versions.sql:21-27`) and stay `STRING`.

| Option | Per-value ceiling | Fit for the ledger |
|---|---|---|
| `STRING` (current) | Arbitrary length; practically bounded only by Spark's ~2 GB single-value ceiling | **Chosen** — matches the optimizer; compresses well; no query restrictions |
| `VARIANT` | Hard **16 MiB** (128 MiB only on DBR ≥17.1) → `VARIANT_SIZE_LIMIT` error | Rejected — hard cliff **and** can't `GROUP BY`/`ORDER BY`/`DISTINCT`/set-op/partition/cluster on it |

`VARIANT`'s only advantage is native `:`/path querying inside the JSON — which we don't
need, because everything we filter or dedupe on is already extracted into typed sidecar
columns (`config_fingerprint`, `state_digest`, `origin`, `response_envelope_digest`, …).
Refs: [VARIANT limitations](https://docs.databricks.com/aws/en/tables/features/variant),
[VARIANT_SIZE_LIMIT report](https://community.databricks.com/t5/data-engineering/variant-size-limit-cannot-build-variant-bigger-than-16-0-mib-in/td-p/133563).

### 16.3 The real ceiling is the write path, not Delta storage — the 16 MiB anchor

The app has no Spark; it writes via the **SQL Statement Execution API**
(`POST /api/2.0/sql/statements`, `platform/live_seams.py`), passing each JSON payload as a
**bound parameter**, not spliced into SQL text. So the binding constraint is the API request,
not the Delta cell:

| Limit | Value | Binds CUJ-1? |
|---|---|---|
| Delta `STRING` cell | ~2 GB (Spark) | No — orders of magnitude above any config |
| `VARIANT` value | 16 MiB (128 MiB on DBR ≥17.1) | Only if we used VARIANT (we don't) |
| **SQL Statement Execution API** | **"maximum query text size is 16 MiB"**; ≤256 bound params | **Yes — the real one** |

The **16 MiB** figure is the non-arbitrary anchor (it happens to coincide with the legacy
VARIANT cap). Source:
[Execute a SQL statement — API reference](https://docs.databricks.com/api/workspace/statementexecution/executestatement).
Note the 25 MiB / 100 GiB numbers in that doc are **result-set** limits (reads), not write
limits.

### 16.4 Derived size-guard (replaces the 32 KB Volume trigger)

Replace `inline_max_bytes=32768`-and-fork with a **loud tripwire** derived from the 16 MiB
request budget, divided by the number of large JSON copies one `INSERT` carries:

- **Variant A — always inline the envelope (recommended now):** one row binds 3 large JSON
  params (`serialized_space_json` + `canonical_state_json` + `response_envelope_json`) plus
  `restorable_metadata_json`. Guard ≈ 16 MiB ÷ ~3 − overhead ≈ **~4–5 MiB per config**.
  Smallest change: schema and read path are untouched; the `envelope_present` CHECK
  (`01-versions.sql:44`) is satisfied because `response_envelope_json` is non-null and
  `response_envelope_uri` is null.
- **Variant B — stop persisting the raw envelope (optional later):** keep only
  `response_envelope_digest`; store 2 large copies. Guard rises to **~7 MiB**. Requires a
  Snapshot-contract + read-path change and dropping/relaxing the `envelope_present` CHECK.

The guard is a **defensive tripwire, not an operating limit.** Genie's own validity rules
(`schema.md:207-213`: ≤100 instructions, individual strings ≤25,000 chars, array items
≤10,000) cap a realistic config at **5–50 KB typical, 50–300 KB for a large space** — 1–2
orders of magnitude below the guard. If the guard ever trips, that is a signal (a malformed
or pathological config), and the right response is a clear error, not a silent Volume divert.

### 16.5 On-disk footprint

Two distinct numbers: **logical** (`octet_length(...)`, what counts against the 16 MiB
request budget) vs **on-disk** (Delta/Parquet, Snappy/ZSTD). Genie config JSON is highly
repetitive, so on-disk is typically **3–10× smaller** than the raw JSON (a 100 KB config is
~10–30 KB on disk). Storage is a non-concern; only the write-request size is worth guarding.
Measure with:
```sql
SELECT version_id,
       octet_length(serialized_space_json)  AS ss_bytes,
       octet_length(canonical_state_json)   AS canon_bytes,
       octet_length(response_envelope_json) AS env_bytes,
       response_envelope_uri
FROM   `${catalog}`.`${control_schema}`.`genie_space_versions`
ORDER BY observed_at DESC LIMIT 25;
```

### 16.6 Concrete changes (when implemented — one PLAN→VERIFY slice)

1. `ledger.py:126-138` — replace `_publish_envelope`'s Volume fork with a size-guard: encode
   the envelope, and if it exceeds the guard, raise a loud `ValueError` naming the space and
   the measured/allowed bytes; otherwise return `(inline_json, None)`. Never call
   `write_artifact`/`read_artifact` on the CUJ-1 path.
2. `ledger.py:80-96` — retire `inline_max_bytes=32768`; introduce the guard constant
   (~4–5 MiB, Variant A). Stop constructing `volume_prefix` for CUJ-1; drop the
   `write_artifact`/`read_artifact` seams from the CUJ-1 wiring
   (`platform/observe_seams.py:169-173`).
3. Add a ledger test: an envelope **> 32 KB** round-trips **inline** with no `write_artifact`
   call; an envelope **> guard** raises the loud error.
4. Refresh live-claim comments and the `vc_snapshots` mentions in this doc (§15.1, §15.2 #1,
   §15.5g) and in `scripts/version_control/provision_observe_nonprod.py`; decide whether to
   drop `vc_snapshots` Volume provisioning for a CUJ-1-only deploy.
5. Fixes the nonprod failure directly: the Aircraft agent's `response_envelope` exceeded the
   32 KB cap → Volume fork → `write_artifact` wrote a **relative** (non-`/Volumes/`) path
   (`ledger.py:132`) → `files.upload` failed, masked by an SDK logger bug (`len(BytesIO)`).
   Removing the fork removes both the relative-path bug and the SDK-logger crash for CUJ-1.

### 16.6.1 Landed (Variant A slice)

`_publish_envelope` now always inlines the envelope and raises a loud
`ValueError` above a `_INLINE_ENVELOPE_GUARD_BYTES = 4 MiB` guard
(`ledger.py`); the default `inline_max_bytes` moved from 32 KB to that guard. The
two Volume-fork ledger tests were replaced by an inline-over-32-KB regression
test and a loud-guard test (`test_vc_ledger.py`). Schema and the `_response_envelope`
read path are untouched; `write_artifact`/`read_artifact`/`volume_prefix` remain on
the ledger (dormant on the write path, still serving legacy `_uri` reads).
**Deferred** to a follow-up slice (kept out to minimise this diff): dropping the
now-unused artifact seams from the CUJ-1 wiring (`observe_seams.py:169-173`) and
removing `vc_snapshots` Volume provisioning
(`scripts/version_control/provision_observe_nonprod.py`) for a CUJ-1-only deploy.

### 16.7 CUJ-2 note

If cross-workspace promotion is kept, its large-artifact handling (`promotion/store.py`,
`releases.py` absolute `/Volumes/...` paths) is a **separate** path and may retain a Volume —
but it must not be reached from the CUJ-1 capture/restore code. This decision governs CUJ-1
inline persistence only.

## 17. Space "Version Control" tab — UX improvement plan

**Status:** Planned, not implemented (decided to write down first) · **Surface:**
`frontend/src/components/version-control/SpaceVersionControlTab.tsx` and its children
(`history.tsx`, `version-detail-panel.tsx`, `config-view.tsx`, `diff.tsx`).

### 17.1 Problems observed (with root cause in code)

1. **Capture lag with no feedback.** On open, `syncOnOpen` (`SpaceVersionControlTab.tsx:69`)
   calls `api.spaceObserve` — the slow part is the OBO live GET of the Genie space inside
   `capture_on_open` — and **swallows everything silently** (no spinner/message) until
   `load()` returns. The manual `capture()` (`:186`) only branches on `captured_version`
   truthy, conflating *no-change*, *busy*, and *permission error* into one message.
2. **Config view too narrow + text overflow.** The tab grid is inverted:
   `grid-cols-[minmax(0,1fr)_minmax(320px,380px)]` (`:267`) makes the **history list wide**
   and crams the **config/detail into a 320–380px sticky rail**. `ConfigView` is fine — it is
   starved of width. Long UC join identifiers overflow (`config-view.tsx:160-161` lack
   `break-all`); `SqlCodeBlock` `overflow-x-auto` scrolls sideways, painful at 320px.
3. **No list multi-select for diff.** Diff is dropdown-driven (`history.tsx:186-202` → two
   `<select>`s → `SemanticDiffView` below the grid).

Backend already distinguishes the outcomes we need: `ObservationResult`
(`types/version-control.ts:30`) carries **`captured_version`** *and* **`busy`**. Observe is a
single blocking POST (`vc_spaces.py:105` `space_observe`) — no SSE today.

### 17.2 Direction — master–detail (list-detail) pattern

Adopt the pattern used by mature version-history UIs (VS Code Timeline, GitKraken, Google
Docs "Version history", Notion, Figma): a **narrow selectable list rail** + a **wide detail
pane with a sticky metadata header over an independently scrollable body**. This is the
inverse of today's layout and directly fixes #2/#3.

### 17.3 Slices (each its own PLAN → VERIFY → commit)

- **Slice A — capture status & staged messaging (fixes #1). Decided: client-driven. LANDED.**
  Make on-open capture visible (status line + rail skeleton). Distinct terminal states from
  `ObservationResult` + errors (Nielsen "visibility of system status"): *saved new version
  (changes detected)* · *no changes since last version* · *capture already in progress*
  (`busy`) · *capture not enabled* (503 `vc_writes_disabled`) · *no access to read this
  space* (permission). Auto-dismiss success/no-change; keep errors sticky. Honest single
  "reading & comparing…" phase now (observe is one blocking call); **no SSE**. Absorbs the
  previously-pending messaging slice. Implemented: `capture-notice.ts`
  (`describeObservation`/`describeCaptureError`), visible `syncing` status +
  `loading || syncing` rail skeleton and tone-styled auto-dismissing notices in
  `SpaceVersionControlTab.tsx`, Slice D folded in (`version-format.ts` `workbench` →
  "Auto-captured", header auto-capture copy, softer empty state). Tests:
  `SpaceVersionControlTab.test.tsx`.
- **Slice B — layout inversion + overflow (fixes #2 & #3). LANDED.** Flipped to
  `lg:grid-cols-[320px_minmax(0,1fr)]`: narrow versions rail left, wide detail right. Detail
  (`version-detail-panel.tsx`) is now `flex h-full flex-col` — a `shrink-0` header (origin
  badge, short id/copy, captured time, actor, fingerprints, lineage, Restore) over a
  `flex-1 min-h-0 overflow-auto` body (sectioned `ConfigView`). Both panes `lg:h-[70vh]` and
  scroll on their own; stack below `lg`. Join identifiers got `break-all`/`min-w-0`
  (`config-view.tsx`) to kill the overflow. `SqlCodeBlock` keeps its horizontal scroll
  (comfortable at the new width).
- **Slice C — multi-select compare in the rail (fixes #4). LANDED.** Checkbox per rail row
  (`history.tsx`, additive `compareIds`/`onToggleCompare` — legacy dropdown kept only for the
  M02 demo). Single-click still opens detail; checkboxes drive compare (avoids the click-nav
  vs multi-select ambiguity — GitHub/Google Docs pattern). The space tab holds a rolling
  max-two `compareIds`; at exactly two selected the right pane switches from version detail to
  a compare panel (`Comparing X ↔ Y`, Clear) rendering the existing `SemanticDiffView`.
  Restore-preview diff is a separate state, untouched.
- **Slice D — labels & copy (fold into A).** Option A relabel `workbench` origin →
  **"Auto-captured"** in `version-format.ts` `ORIGIN_META`; explanatory copy (auto-capture
  points = optimizer runs + entering the workbench; direct-in-Genie edits captured on next
  open; no background watcher); soften the empty state.
- **Slice E (optional, deferred) — true server-side stages via SSE.** Stream
  `reading → canonicalizing → comparing → captured/no-change` from a new `observe/stream`
  endpoint reusing the create-agent SSE infra. Only if Slice A's client-side status is
  insufficient. **Not chosen now.**

**Suggested order:** A → B → C, D folded into A. Recommended when implementation resumes.

---

## 18. ConfigView redesign — navigable, clearly-labeled Genie config

The right-pane `ConfigView` (`frontend/src/components/version-control/config-view.tsx`,
rendered by `version-detail-panel.tsx`) is the surface a reviewer reads to understand a
captured version. Today it stacks a few flat sections; for a real space (e.g. the airline
demo: 7 data sources, ~20 columns each) it becomes a wall of text. Authoritative shape:
`backend/references/schema.md` (serialized_space v2).

**Problems (grounded in current code):**
- Tables and metric views are merged and unlabeled (`config-view.tsx` `[...tables,
  ...metricViews].map`).
- Columns are always a flat inline list with none of the schema's per-column metadata
  (`synonyms`, `exclude`, `enable_entity_matching`, `enable_format_assistance`,
  `get_example_values`, `build_value_dictionary`).
- Distinct concepts collapse into one "Sample SQL" bucket (example SQLs + expressions +
  measures + sql_functions); example SQL `parameters`/`usage_guidance` are dropped.
- Instructions are a raw `<pre>`; join `--rt=…--` relationship annotations leak into the SQL
  body; `benchmarks.questions` are not surfaced; there is no in-config navigation.

**Nuance:** in serialized_space a metric view is a data source referenced by `identifier` +
`column_configs` (its dimensions/measures surface as columns). Space-level
measures/filters/expressions live under `instructions.sql_snippets`, NOT inside the metric
view — so "Metric views" is labeled distinctly but its body still shows columns; Measures is
a separate section.

**UI direction (researched):** schema-browser tree + progressive disclosure (VS Code
Explorer, DBeaver, UC schema browser) for entities→columns; detail-on-the-side peek
drawer/popover (Linear/Notion) for a column's full metadata; anchored TOC / jump-nav (docs
sites, Stripe API ref) for navigation; attribute badges + tooltips for booleans. Prefer
stacked sections + jump-nav over tabs for a *review* surface. Reuse existing primitives:
`ui/accordion.tsx` (`AccordionItem`), `ui/collapsible.tsx`, `ui/tabs.tsx`, `ui/tooltip.tsx`,
`ui/table.tsx`, `ui/badge.tsx`, `SqlCodeBlock`. Keep the `unknown`-safe reads
(`asObject`/`asArray`/`text`), empty-section omission, unknown-key "Other", and raw-JSON
disclosure.

**Slices (each PLAN → VERIFY → commit):**
- **Slice 1 — split & label (this slice).** Separate **Tables** and **Metric views**; break
  "Sample SQL" into **Measures**, **Expressions**, **Filters**, **SQL functions**, and
  **Example SQL** (with `parameters` + `usage_guidance`); add **Benchmarks**
  (`benchmarks.questions` + expected SQL answer); surface `sample_questions` from
  `config.sample_questions` too. Snippet sections show `synonyms`/`instruction`/`comment`.
  Columns stay flat for now. Pure restructuring, lowest risk. Tests extend
  `config-view.test.tsx` with a full schema-shaped fixture.
- **Slice 2 — collapsible entities + column attribute badges. LANDED.** Data source entities
  are collapsible cards (`AccordionItem`): header = identifier + description + `N cols · M
  excl`; body reveals columns with per-column attribute badges (`Excluded` / `Entity match` /
  `Format assist` / `Example values` / `Value dict`, explained via native `title`) plus full
  description + synonyms. `defaultOpen` when `1 ≤ columns ≤ 8` so big tables (e.g. airline
  `certifying_staff`) collapse; the body stays in the DOM when collapsed so it remains
  searchable. The full-detail "column peek" was realized as this in-card expansion (inline
  badges + detail) rather than a separate side drawer — chosen for robustness (no portal/
  overlay); a true side drawer remains an option if deeper per-column metadata is needed.
- **Slice 3 — in-config section nav.** Sticky section index / two-column at `xl`, chip bar or
  jump dropdown below; anchors scroll-to.
- **Slice 4 (polish).** Parse GSL instruction headers (## PURPOSE / DISAMBIGUATION / …) into
  headings; relationship-type badge from `--rt=…--`.
