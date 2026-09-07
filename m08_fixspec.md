# M08 Fix Specification — Provisioning / Identity / Integration

**Verdict: BLOCK** (5 blocking findings, all remediable; none require re-architecture)

**Reviewer:** Claude (Anthropic) — independent cross-vendor review. Implementer ran on GPT/Astra.
**Authority:** `docs/design/version_control/implementation_plan/module-08-provisioning-identity.md`,
`contracts.md` (VC/1.0), `testing-strategy.md`.
**Subject:** `.polly/m08.diff` (24 files, 2000 lines).

---

## Correction to the prior review (B1 top-line claim withdrawn)

My earlier review asserted that *"three of the diff's own new tests assert on strings that do not
exist in the files the diff produces, so the 20 passed cannot be passing."* **That conclusion was
wrong and is withdrawn in full.**

The error was methodological: I resolved those string assertions against the **pre-application base
repo** (HEAD `e9527ca`, where the diff hunks are by definition not yet applied) rather than against
the **applied worktree**. In the applied worktree the strings are present and correctly ordered:

| Assertion | Applied-worktree fact |
|---|---|
| `deploy.sh`: guard precedes `DEPLOYER=` | `_preflight_check_vc_bundle_content` at line 179 < `DEPLOYER=$(databricks current-user` at line 185 ✅ |
| `install.py`: preflight precedes `ensure_app` | `preflight_deployment(Path(cfg.repo_root or "")` at line 45 < `ensure_app(w, cfg)` at line 52 ✅ |

The M08 unit suite genuinely passes (**20 passed, 5 deselected**). The tests are real, the assertions
are satisfiable, and the recorded evidence is honest. Any "unsatisfiable assertion,"
"self-refuting evidence," or "fabricated pass" framing from the prior review is retracted and must
not be carried into remediation. **B1 is accordingly downgraded from a blocking evidence-integrity
finding to a hardening item**, retained only for its two substantive parts (helper-ordering
robustness in `preflight.sh`, and a coverage gap in the ordering assertion itself).

The BLOCK verdict rests on **B2–B5**, which are independent of the withdrawn claim, plus the
still-valid hardening in B1.

---

## What is genuinely strong — do not regress while fixing

These are the hard parts of M08 and they are done correctly. Every fix below must preserve them.

1. **Master write gate is correct and layered.** `FeatureFlags.enabled()` requires the specific
   switch **AND** `vc_writes_enabled`. `Composition.enabled()` additionally requires all 9
   `REQUIRED_WRITE_PORTS` registered, all 7 `REQUIRED_WRITER_PATHS` routed, and all 18
   `FIRST_WRITE_CAPABILITIES` live-probed true. All five `app.yaml` switches ship `"false"`. A probe
   exception yields `False` with **no cached success**. Satisfies M08 §6 "first write-enabled
   milestone" and FM-OUTAGE.
2. **DABs are provision-only; dual authority is detected.** No `resources.genie_spaces` anywhere
   (confirmed absent from both pre-existing bundles). `guard_bundle` walks nested includes with cycle
   detection and rejects unresolvable globs. `content_fingerprint()` proves governed Genie content is
   byte-identical across two provision cycles. Satisfies FM-DUAL-AUTHORITY.
3. **`backend/jobs/build_vc_runtime` refuses by default** — `raise PermissionError(...)`, no stub
   handlers. This is the single most tempting place to fabricate integration and it was not taken.
4. **Identity is fail-closed and explicit.** `DEFAULT` profile rejected; OAuth M2M for SP / OBO-`pat`
   for user with no cross-fallback; live `X-Databricks-Org-Id` preferred over cached SDK config;
   `canonical_host` rejects non-HTTPS, embedded credentials, paths, and ports. Satisfies F-IDENTITY
   (no startup authority repair, no default production profile, no viewer elevation).
5. **Jobs take `operation_id` only** and load the durable request themselves via `durable_request()`,
   validating canonical UUID, target-locality, and kind match. Mutation tasks `max_retries: 0`; only
   `verification` gets `2`. Unresolved/terminal attempts route to `verify_only`, never re-mutate.
6. **No `.sql` authored** (M08 authors no DDL — confirmed zero `.sql` files in the diff); **no
   GitHub Actions, external runner, or Git-as-operational-ledger**; all 24 changed paths fall inside
   the M08 ownership manifest.
7. **Integration tests are honestly marked.** `addopts = "-m 'not integration'"` deselects by
   default; `live_platform` calls `pytest.fail("DEPLOYMENT BLOCKER: ...")` rather than
   `pytest.skip` when unconfigured — a skip would have been the easy dishonest choice. `TDD.md`
   repeatedly and specifically records "NOT EXECUTED, deployment blocker" and records platform
   prerequisites as blockers rather than passes.

**Ownership reminder for all fixes.** M08 owns root `pyproject.toml`, `requirements.txt`, `app.yaml`,
`databricks.yml`; `scripts/*`; `backend/main.py`, `backend/models.py`, `backend/services/auth.py`;
`backend/services/version_control/platform/*`; `backend/jobs/__init__.py`;
`backend/tests/{test_vc_provisioning,test_vc_identity,test_vc_composition}.py`; and
`backend/tests/integration/conftest.py`. M08 **authors no `.sql`** — it executes owner migrations and
grants only. Every fix below stays inside this manifest.

---

### B1 — Guard helper defined above its own status helpers; ordering assertion under-covers

*(Downgraded to hardening — not an evidence-integrity finding. See the correction above.)*

**(a) Exact location**
- `scripts/preflight.sh` — new `_preflight_check_vc_bundle_content()` inserted at ~line 13, **above**
  the color/status-helper block (`RED`/`GREEN`/`NC` at lines 14–20, `_error()` at line 25).
- `scripts/deploy.sh` line 179 — guard call site.
- `backend/tests/test_vc_provisioning.py`, in
  `test_bundle_inventory_guard_rejects_governed_content_and_exports_detection_evidence` — the
  ordering assertion.

**(b) Defect and violated invariant**

Two separate hardening gaps; neither is a live bug today.

1. *Definition ordering.* The new function calls `_error` and reads `RED`/`NC`, all defined **below**
   it. Bash resolves function bodies at call time, so this works today — the guard is invoked from
   `deploy.sh` long after the whole file is sourced. But it makes the file's only forward reference,
   and any future refactor that moves the call earlier, or sources a subset of the file, breaks it
   silently and prints an unstyled/empty error at exactly the moment an operator most needs a legible
   refusal.
2. *Assertion under-coverage.* The test pins the guard only against
   `DEPLOYER=$(databricks current-user` (line 185). That is one of **~40** `databricks` invocations in
   `deploy.sh`. The assertion therefore cannot detect a guard that runs after some other platform
   call — which is precisely the regression B3(b) turns out to be a live instance of (the `--destroy`
   branch reaches `databricks bundle destroy` at line 125 and `exit 0`s at line 154, before line 179
   is ever evaluated). A correctly-scoped ordering assertion would already have caught B3(b).

Violated invariant: M08 §6 FM-DUAL-AUTHORITY — *"sanctioned deploy content guard plus inventory feed
for one-cycle detection."* A guard whose ordering is only spot-checked is not a one-cycle guarantee.

**(c) Concrete fix**
1. In `scripts/preflight.sh`, move the `_preflight_check_vc_bundle_content()` definition to **below**
   the shared color + status helper block (i.e. after `_header()` at line 26), alongside the other
   `_preflight_*` functions. Pure relocation; no body change.
2. Strengthen the ordering assertion to enumerate **every** `databricks ` invocation in `deploy.sh`
   (excluding comment lines and echoed remediation hints) and assert the guard call precedes all of
   them **on every reachable control-flow branch**, including `--destroy`.

**(d) Named RED test**

`test_bundle_guard_precedes_every_databricks_invocation_on_every_branch`
in `backend/tests/test_vc_provisioning.py`.

- Assert in `preflight.sh` that `index("_error()")` < `index("_preflight_check_vc_bundle_content()")`.
- Parse `deploy.sh`; collect line numbers matching `(^|[^#\w])databricks ` with comment lines and
  `echo`-only remediation strings excluded; assert `min(those) > index(guard call)`.
- RED today on the `--destroy` branch (guard at 179 > `bundle destroy` at 125), which is the B3(b)
  failure — this single test covers both findings.

---

### B2 — PyYAML imported but undeclared; `platform/__init__.py` contradicts its own lazy contract

**(a) Exact location**
- `backend/services/version_control/platform/bundles.py` line ~178 — top-level `import yaml`.
- `backend/services/version_control/platform/__init__.py` lines 97–103 — seven eager submodule
  imports (`bundles`, `provisioning`, `permissions`, `capabilities`, `identity`, `jobs`,
  `termination`) plus `from .. import contracts`.
- `scripts/deploy_lib/install.py` line ~21 —
  `from backend.services.version_control.platform import preflight_deployment`.
- Root `pyproject.toml` `[project].dependencies` — **`pyyaml` absent**.

**(b) Defect and violated invariant**

`bundles.py` imports `yaml` at module scope, and `platform/__init__.py` imports `bundles`
**eagerly**, so `import backend.services.version_control.platform` hard-requires PyYAML. Verified:

```
grep -n "pyyaml" pyproject.toml                        ->  (absent)
packages/genie-space-optimizer/pyproject.toml:11:  "pyyaml==6.0.3"
```

PyYAML is reachable today only **transitively**, via the root's dependency on
`genie-space-optimizer`. Two consequences:

1. *Undeclared direct dependency.* `install.py` now imports this package on the notebook-installer
   path. `notebooks/install.py` runs inside a Databricks notebook against a **generated workspace
   source folder**, which is not guaranteed to carry the GSO wheel's transitive closure. A missing
   `yaml` there is an `ImportError` during install, not a legible refusal. M08 exclusively owns root
   `pyproject.toml` and had every right to add the pin; it did not.
2. *Docstring/behavior contradiction.* The pre-diff docstring read *"Lazy, explicitly injected VC
   composition. No global clients or router mounts."* The diff replaced it with *"Fail-closed VC/1.0
   platform adapters"* — accurate for content — while converting the package to seven eager imports.
   `Composition` still advertises laziness, so `install.py` asking for one pure-function
   (`preflight_deployment`) now transitively pulls `identity`, `jobs`, and `contracts` into a
   deploy-time process that needs none of them, widening the blast radius of any import-time error in
   the whole platform subtree.

Violated invariants: M08 §2 (M08 owns root `pyproject.toml`/`requirements.txt` and *"owns dependency
addition requests from all modules; modules submit required import/version/test evidence, not root
manifest patches"*); `CLAUDE.md` dependency-security policy (*exact versions only; lockfiles must
validate*); M08 §3 (composition is lazy and explicitly injected).

**(c) Concrete fix**
1. Add `"pyyaml==6.0.3"` to root `pyproject.toml` `[project].dependencies` — exact pin, matching the
   version already resolved for GSO so the lock does not fork.
2. Regenerate the lock and pip reference per `CLAUDE.md` (do **not** hand-edit `requirements.txt`):
   ```bash
   uv lock --upgrade-package pyyaml
   uv export --frozen --no-dev --no-hashes --format requirements-txt \
     | grep -v "^-e " > requirements.txt
   echo "-e ./packages/genie-space-optimizer" >> requirements.txt
   ```
3. Make `platform/__init__.py` genuinely lazy — replace the seven eager `from .x import y` lines with
   a module-level `__getattr__(name)` that resolves and caches on first access (PEP 562), keeping the
   public surface identical so no consumer import changes.
4. In `scripts/deploy_lib/install.py`, import from the leaf module directly:
   `from backend.services.version_control.platform.bundles import preflight_deployment` — the
   installer then needs only `bundles` + `yaml`, never `identity`/`jobs`/`contracts`.

**(d) Named RED test**

`test_platform_package_import_requires_only_declared_root_dependencies`
in `backend/tests/test_vc_provisioning.py`.

- Parse root `pyproject.toml`; assert `pyyaml==6.0.3` is present in `[project].dependencies` with an
  exact `==` pin (RED today — absent).
- Assert every third-party module imported at `platform` package-import time appears in the declared
  root dependency set.
- Assert laziness: in a subprocess, `import backend.services.version_control.platform` and assert
  `backend.services.version_control.platform.identity` and `...jobs` are **not** yet in `sys.modules`;
  then touch `platform.PlatformIdentityProvider` and assert `identity` is now loaded.
- Assert `scripts/deploy_lib/install.py` imports `preflight_deployment` from `...platform.bundles`,
  not from the package root.

---

### B3 — Bundle guard uses bare `python3`, conflates tooling failure with a real finding, and is absent from `--destroy`

**(a) Exact location**
- `scripts/preflight.sh` — `_preflight_check_vc_bundle_content()` body: `python3 "$root/scripts/version_control/bundle_guard.py" ...`.
- `scripts/version_control/bundle_guard.py` lines ~14–24 — `main()` returns `0` / `1` only.
- `scripts/deploy.sh` lines 72–155 — the `--destroy` branch: `databricks bundle destroy -t app` at
  line 125, `exit 0` at line 154. **The guard call at line 179 is unreachable from this branch.**

**(b) Defect and violated invariant**

Two defects; (b2) is the only finding in this review I would classify as a genuine safety hole rather
than an integration defect.

1. *Interpreter mismatch conflates tooling failure with a governed-content finding.* `preflight.sh`
   invokes bare `python3`. The repo's dependency contract installs into `.venv` via
   `uv sync --frozen` (`_preflight_check_venv`); system `python3` is outside that contract. Combined
   with B2, `python3 -c "import yaml"` can fail on a clean operator machine, and `bundle_guard.py`
   exits non-zero for a **tooling** reason. `preflight.sh` maps *any* non-zero exit to
   `_error "VC bundle content guard failed; refusing deployment."` + `exit 1`. Failing closed is
   correct, but the operator cannot distinguish *"your bundle contains governed Genie content"* from
   *"PyYAML is missing."* `TDD.md` itself insists tooling errors "are NOT counted as behavioral RED,"
   yet the shipped guard cannot make that distinction at runtime — the exact conflation M08 exists to
   prevent.
2. *`--destroy` has no guard.* Verified control flow: `--destroy` enters at line 72, runs
   `databricks bundle destroy -t app` at line 125, and `exit 0`s at line 154 — never reaching line
   179. A bundle carrying a `resources.genie_spaces` block would be **destroyed through DAB** without
   tripping the guard. `bundle destroy` against governed content is a *destructive* dual-authority
   action, strictly worse than the deploy case the guard does cover.

Violated invariants: M08 §6 FM-DUAL-AUTHORITY (one-cycle detection); `contracts.md` §"Definition of
done" (*"no … DAB-owned content"*); M08 §6 (*"App and operational machinery provision via DAB; no
external CI/CD control plane"* — a guard bypassed on teardown is not a provisioning boundary).

**(c) Concrete fix**
1. Invoke the guard with the project interpreter, not the system one — prefer
   `"${PROJECT_DIR}/.venv/bin/python"` when present, else `uv run python`, and fail with a distinct
   message if neither is available.
2. Give `bundle_guard.py` three exit codes and document them in its docstring:
   - `0` — all bundles `clear`
   - `1` — at least one `dual_authority` finding (**refuse the deployment**)
   - `2` — tooling/unresolvable input (missing interpreter dep, unreadable file, `status == "unknown"`)
     — still refuse, but report it as an environment problem, not a governed-content finding.
   Map `status == "unknown"` (already produced by `bundle_detection_evidence` for missing includes) to
   `2`, and `status == "dual_authority"` to `1`.
3. Have `_preflight_check_vc_bundle_content` branch on the exit code and emit two distinct
   `_error` messages.
4. Add `_preflight_check_vc_bundle_content` to the `--destroy` branch, **before** the
   `databricks bundle destroy` call at line 125.

**(d) Named RED test**

`test_bundle_guard_runs_on_destroy_and_separates_tooling_failure_from_dual_authority`
in `backend/tests/test_vc_provisioning.py`.

- Assert `deploy.sh`'s `--destroy` branch (lines between `DESTROY_MODE" = "true"` and its `exit 0`)
  contains a `_preflight_check_vc_bundle_content` call textually **before** `databricks bundle
  destroy` (RED today — no call in that branch).
- Run `bundle_guard.py` in a subprocess over a `tmp_path` bundle containing
  `resources: {genie_spaces: ...}`; assert returncode `1` and `"dual_authority"` in stdout.
- Run it with `PYTHONPATH` scrubbed / `yaml` shadowed so the import fails; assert returncode `2`,
  **not** `1` (RED today — `main()` cannot return 2).
- Run it over a clear bundle; assert `0`.
- Assert `preflight.sh` does not invoke bare `python3` for the guard (regex-assert `.venv/bin/python`
  or `uv run python`).

---

### B4 — `verify_configured_job_run_as` hard-fails app startup on the unsubstituted `__GSO_JOB_ID__` placeholder

**(a) Exact location**
- `backend/services/version_control/platform/identity.py`, `verify_configured_job_run_as()` —
  the `if not execution_ref.isdigit() or int(execution_ref) <= 0: raise ValueError(...)` branch.
- `backend/main.py` — `_verify_gso_job_run_as()` is the **first statement** of `startup()`, ahead of
  `init_pool()` and `_ensure_table()`.
- `app.yaml` line 94 — ships `value: "__GSO_JOB_ID__"`.
- `scripts/deploy.sh` lines 478 (`if [ -n "$JOB_ID" ]` guards substitution) and 484–488 (unresolved
  placeholders **warn only**, no `exit`).

**(b) Defect and violated invariant**

`app.yaml` ships the literal placeholder `"__GSO_JOB_ID__"` — non-empty and non-digit. `deploy.sh`
substitutes it only `if [ -n "$JOB_ID" ]`, and when substitution fails it merely prints
`⚠ app.yaml has N unresolved placeholder(s)` and **continues to `apps deploy`** (verified: no `exit`
in that block). So a deploy where `JOB_ID` failed to resolve ships `GSO_JOB_ID=__GSO_JOB_ID__` to the
app, where:

`environment.get("GSO_JOB_ID")` → `"__GSO_JOB_ID__"` → non-empty, so the early `return` is skipped →
not `.isdigit()` → **`ValueError`** → raised from the first line of `startup()` → **the entire app
fails to boot.**

The replaced code logged a warning and continued. This converts a *degraded-optimizer* condition into
a *total application outage*, and it is reachable on the documented notebook-install path where
`GSO_JOB_ID` is injected by a separate step.

The intent is right: M08 §5 task 8 demands verify-and-refuse with no self-healing, and refusing on a
**wrong** `run_as` is exactly correct. The defect is over-triggering: refusing on an **unconfigured**
one. "Not configured" and "configured but wrong" are different states and only the second is an
authority violation.

Violated invariant: `testing-strategy.md` FM-OUTAGE — *"All mutations stop without
coordination/evidence; **available history reads continue**."* A hard boot failure takes down history
reads, the frontend, and every unrelated surface. (F-IDENTITY's "no startup authority repair" is
satisfied and must stay satisfied — the fix must not reintroduce repair.)

**(c) Concrete fix**
1. In `verify_configured_job_run_as`, treat the literal unsubstituted placeholder **and any
   non-digit value** as *"optimizer integration not configured"* → `logger.warning(...)` and
   `return`, matching the existing empty-string path. Reserve `ValueError`/`PermissionError`
   exclusively for a **configured** (all-digits, positive) job whose `run_as` is wrong, or whose
   identity cannot be read. Preserve the existing behavior for that case exactly: still raise, still
   never call `jobs.update`/`jobs.reset`.
2. In `scripts/deploy.sh`, make the unresolved-placeholder check at lines 484–488 **exit non-zero**
   instead of warning, so a half-configured `app.yaml` is never imported to the workspace. This is
   the actual root cause; the identity fix is the defense in depth.

**(d) Named RED test**

`test_unconfigured_job_id_placeholder_does_not_block_startup`
in `backend/tests/test_vc_identity.py`.

- `verify_configured_job_run_as({"GSO_JOB_ID": "__GSO_JOB_ID__"}, factory)` returns `None` and
  `factory.assert_not_called()` (RED today — raises `ValueError`).
- Same for `""`, `"none"`, `"0"`, `"-1"`.
- Regression-pin the refusal that must survive: a **configured** `"42"` whose
  `run_as.service_principal_name` is `"wrong-sp"` still raises `PermissionError` matching `run_as`,
  and `client.jobs.update` / `client.jobs.reset` are never called.
- Regression-pin fail-closed reads: `jobs.get` raising `TimeoutError` for a configured job still
  propagates.
- Assert `scripts/deploy.sh` exits non-zero on unresolved placeholders (assert an `exit 1` inside the
  `UNRESOLVED` block).

---

### B5 — The five integration tests live in unit-test files, so the contract's integration command collects zero tests

*(Fully re-derived; this finding was truncated in the prior delivery.)*

**(a) Exact location**
- `backend/tests/test_vc_provisioning.py` — all five `@pytest.mark.integration` tests:
  1. `test_runtime_principal_cannot_update_delete_or_alter_fact_tables` (plan task 5)
  2. `test_executor_cannot_insert_coordination_and_enrollment_is_serialized` (task 6)
  3. `test_package_approval_receipt_securables_have_separate_writers` (task 14)
  4. `test_dab_validate_and_sandbox_provision_are_repeatable` (task 16)
  5. `test_source_target_artifact_topology_is_verified_without_remote_write_credentials`
- `backend/tests/integration/conftest.py` — new; the **only** file in that directory. No
  `__init__.py`.
- `backend/tests/test_vc_provisioning.py` line ~8 —
  `from backend.tests.integration.conftest import live_platform` (cross-directory fixture import).
- `pyproject.toml` — `addopts = "-m 'not integration'"`, `markers = ["integration: ..."]`.

**(b) Defect and violated invariant**

`testing-strategy.md` §"Test layout and commands" pins the release-gate command for this layer:

```
| Real Delta/security | `backend/tests/integration/` / M03, M07, M08 |
  `python -m pytest backend/tests/integration -m integration -q` |
```

The diff places all five integration tests in `backend/tests/test_vc_provisioning.py`, and
`backend/tests/integration/` contains only `conftest.py`. Therefore **the contract's documented
command collects zero tests.** Pytest exits `5` (`EXIT_NOTESTSCOLLECTED`) with "no tests ran" — a
release gate or CI step reading exit codes loosely, or a human skimming for the absence of `FAILED`,
can easily score that as *not failing*. That is precisely the silent-skip failure mode M08 is
chartered to make impossible.

This is the one finding that interacts with the module's core honesty property. The **markers
themselves are honest** — `addopts` deselects by default, and `live_platform` calls
`pytest.fail("DEPLOYMENT BLOCKER: ...")` rather than `pytest.skip` when `VC_INTEGRATION_CONFIG` is
unset, which is the correct and non-obvious choice. `TDD.md` correctly records all five as
"NOT EXECUTED, deployment blocker." So the *evidence claims are sound*; what is broken is the
**gate's reachability** — the honest blockers sit at a path the sanctioned command does not traverse,
so the blocker cannot be enforced by the documented command.

`TDD.md` acknowledges the deviation: *"Integration tests live in the three owned `test_vc_*.py` files
(not the plan's unowned `integration/test_vc_permissions.py`)."* **The stated justification is
factually wrong on the authority question.** M08 §2 grants, verbatim:

> `backend/tests/integration/conftest.py`, `test_vc_permissions.py` **if new**; root test
> configuration only when required

`backend/tests/integration/test_vc_permissions.py` does not exist in the base repo, so it *is* new,
so M08 **owns** it. The plan also names it explicitly as the destination for tasks 5, 6, and 14. No
ownership constraint forced the deviation; it was avoidable.

Two secondary structural problems follow:
- **Cross-directory fixture import.** `from backend.tests.integration.conftest import live_platform`
  resolves (I verified PEP 420 namespace-package import works here), but pytest applies a
  `conftest.py` only to tests **at or below its own directory**. Importing the fixture object by name
  into a sibling module works by accident of it being a plain module-level object, not by pytest's
  fixture-discovery contract — brittle against any future `conftest`-scoped hook, plugin, or
  `--import-mode` change.
- **Missing `__init__.py`.** `backend/tests/__init__.py` exists, so the suite uses package-style test
  layout; `backend/tests/integration/` omits it, mixing layouts within one suite.

Violated invariants: `testing-strategy.md` §"Test layout and commands" (integration layer lives at
`backend/tests/integration/`); §"Definition of done" (*"Real-platform capability/permission/CAS tests
have recorded environment and results; unavailable integrations are explicit blockers to affected
feature enablement, **not silent test skips**"*); M08 §5 tasks 5, 6, 14 (named destination
`integration/test_vc_permissions.py`).

**(c) Concrete fix**
1. Create `backend/tests/integration/test_vc_permissions.py` (**M08-owned**) and move tests 1–3 and 5
   there — the grant/permission/topology probes the plan assigns to that file.
2. Move test 4 (`test_dab_validate_and_sandbox_provision_are_repeatable`) to
   `backend/tests/integration/test_vc_provisioning_live.py`, keeping the DAB-CLI probe distinct from
   the permission probes. (Plan task 16 says "in `test_vc_provisioning.py` (integration marker)"; if
   colocation there is preferred, keep it — but then the structural test in (d) must assert that file
   is *also* reachable from the sanctioned command, e.g. by adding
   `backend/tests/integration` **and** the marker to a documented composite gate command. Preferring
   the move keeps one command authoritative.)
3. Delete the `from backend.tests.integration.conftest import live_platform` import from
   `test_vc_provisioning.py`; the relocated tests receive `live_platform` natively from
   `integration/conftest.py`.
4. Add `backend/tests/integration/__init__.py` for consistency with `backend/tests/__init__.py`.
5. Leave `addopts`, the `integration` marker, and the `pytest.fail` blocker semantics **unchanged** —
   they are correct.

**(d) Named RED / structural test**

`test_integration_gate_command_collects_every_integration_test`
in `backend/tests/test_vc_provisioning.py`.

- In a subprocess, run
  `python -m pytest backend/tests/integration -m integration --collect-only -q`; assert the exit code
  is **not** `5` (`EXIT_NOTESTSCOLLECTED`) and that exactly **5** tests are collected (RED today —
  collects 0, exits 5).
- Assert no file under `backend/tests/` **outside** `backend/tests/integration/` contains
  `@pytest.mark.integration` — pins the layout so a future integration test cannot drift back out of
  the gated directory.
- Assert `backend/tests/integration/__init__.py` exists.
- Assert no module outside `backend/tests/integration/` imports `live_platform` from
  `integration.conftest`.
- Regression-pin the honest-blocker semantics: with `VC_INTEGRATION_CONFIG` unset, the
  `live_platform` fixture **fails** (does not skip) — grep `integration/conftest.py` for
  `pytest.fail` and assert `pytest.skip` is absent.

---

## Non-blocking findings

1. **`platform/__init__.py` docstring/behavior mismatch.** The replaced docstring advertised laziness;
   the package now performs seven eager imports while `Composition` still advertises lazy injection.
   Resolved incidentally by B2's `__getattr__` fix — restate the docstring to match.
2. **`Composition.enabled()` re-probes on every call.** `capabilities_ready` invokes `probe()` per
   call with no memoization. This is **correct** — it is what makes *"outages never reuse an earlier
   positive result"* true — but it means a hot write path issues a live probe per check. Add an
   explicit comment saying the absence of caching is load-bearing, so a later optimization pass does
   not "helpfully" add a cache and silently break FM-OUTAGE.
3. **`guard_bundle` matches any dict key named `genie_spaces` at any depth.** Deliberately
   over-broad and fail-closed (good), but it will also reject an unrelated `genie_spaces` key inside
   e.g. a `tags` or `variables` block. Acceptable for v1 — document that false positives are
   intentional and that the remedy is renaming the key, not narrowing the guard.
4. **`termination.py` swallows all exceptions into `None`** (`except Exception: return None`).
   Correct per contract (*"missing proof returns None"*), but it also silences genuine bugs. Add a
   debug-level log before returning so a systematically-broken reader is diagnosable rather than
   indistinguishable from a permanent absence of evidence.
5. **`identity.py` keys verified executors by `id()`** (`self._contexts[id(context)]`). CPython reuses
   addresses after GC, so a freed `ExecutorContext`'s address could in principle be reallocated to an
   unverified one. Practical risk is low because `_client` re-runs the live SCIM check on every call.
   Prefer keying on `(workspace_id, principal_id, execution_ref)` plus a per-instance nonce.
6. **`test_vc_composition.py` lost its concurrency assertion.** The pre-diff version exercised
   `ThreadPoolExecutor(max_workers=4)` over `composition.resolve(...)` and asserted the factory ran
   exactly once; the rewrite replaced it with a two-call `is` check. `resolve` still holds
   `self._lock`, so behavior is intact — but the regression guard for single-instantiation under
   concurrency is gone. Restore the threaded assertion.
7. **`deploy_lib/gso_job.py` now hard-fails on empty `app_sp_client_id`.** Correct and desirable —
   the deployer must set `run_as` explicitly rather than relying on startup self-heal, which is the
   point of plan task 8. `install.py` resolves it from `get_app_service_principal` before the call, so
   the happy path is safe. Flagged only because it changes notebook-install failure timing.
8. **`resources/version-control/platform.jobs.yml` hardcodes wheel versions and adds a second
   `artifacts` block.** `genie_workbench-0.1.0` and `genie_space_optimizer-0.0.0` are pinned by
   filename; the latter depends on root `databricks.yml:38`'s copy-to-fixed-name workaround (GSO uses
   `dynamic = ["version"]`), coupling the two bundles through an undeclared filesystem side effect. A
   root version bump breaks VC Job dependency resolution at **run** time, not deploy time. Also, the
   `include: resources/version-control/*.yml` merge means the resolved root bundle now carries two
   `artifacts` mappings (`default` + `vc_platform`), an interaction never validated live (`TDD.md`
   concedes *"This is NOT live `bundle validate --strict`"*). Derive both wheel names from bundle
   variables and confirm a single merged `artifacts` mapping before `vc-sandbox` is claimed as
   provisioned. **Not blocking** because `vc-sandbox` is opt-in, requires three explicit principals,
   and its entrypoint refuses via `build_vc_runtime` — but it must be closed before any live
   provisioning run is presented as evidence.

---

## Summary

**File written:** `/home/sandbox-agent/workspace/databricks-genie-workbench/.polly/m08_fixspec.md`
(markdown only; no source files edited)

**Verdict: BLOCK** — 5 findings (B1 hardening, B2–B5 blocking), 8 non-blocking.

| # | One-line summary | RED test |
|---|---|---|
| B1 | Guard helper defined above its own `_error`/color helpers; ordering assertion covers only 1 of ~40 `databricks` calls, so it cannot catch B3(b) | `test_bundle_guard_precedes_every_databricks_invocation_on_every_branch` |
| B2 | `bundles.py` imports `yaml` but `pyyaml` is undeclared in root `pyproject.toml` (transitive via GSO only); `platform/__init__.py` eagerly imports 7 submodules, contradicting its lazy contract | `test_platform_package_import_requires_only_declared_root_dependencies` |
| B3 | Guard runs under bare `python3` and cannot distinguish tooling failure from a real `dual_authority` finding; **`--destroy` reaches `databricks bundle destroy` (line 125) and exits (154) before the guard (179)** | `test_bundle_guard_runs_on_destroy_and_separates_tooling_failure_from_dual_authority` |
| B4 | `verify_configured_job_run_as` raises on the unsubstituted `__GSO_JOB_ID__` placeholder from the first line of `startup()` → total app boot failure; `deploy.sh` only warns on unresolved placeholders | `test_unconfigured_job_id_placeholder_does_not_block_startup` |
| B5 | All 5 integration tests sit in unit-test files, so `pytest backend/tests/integration -m integration` collects **zero** and exits 5; M08 in fact owns the plan's `integration/test_vc_permissions.py` destination | `test_integration_gate_command_collects_every_integration_test` |

**Retracted:** the prior review's "unsatisfiable assertion / self-refuting evidence / fabricated
pass" framing on B1. The M08 unit suite genuinely passes (20 passed, 5 deselected) and the asserted
strings are present and correctly ordered in the applied worktree; my analysis had incorrectly
resolved them against the pre-application base repo.

**Severity note:** B3(b) — the unguarded `--destroy` path — is the only finding I would classify as a
genuine safety hole rather than an integration defect. B5 is the most consequential for release
integrity, because it renders honestly-marked blockers unenforceable by the sanctioned command.

**Path to APPROVE:** B1, B4, B5 are mechanical. B2 is a one-line pin plus a `__getattr__` refactor.
B3 is the substantive one. On resubmission, re-review scope is the 5 named RED tests (each
demonstrated RED before GREEN, per `testing-strategy.md` §1) plus verbatim output of the three-file
offline command **and** `pytest backend/tests/integration -m integration --collect-only -q` showing 5
tests collected.
