# M10 task 13: installed optimizer ports

## Installation and offline verification

The package declares `genie_space_optimizer.vc` / `ports` in `pyproject.toml`.
The build backend writes this declaration into distribution entry-point metadata;
no discovery shim or backend startup import is needed. M08 target-local
composition loads `OptimizerRuntime` (`contract_version == "VC/1.0"`) and injects
the champion adapter, restore-job port, and isolation registry. `champion_run`
uses that injected adapter, not an independently constructed mutation gate.

From this package directory, prepare the workspace environment before using
`--no-sync`:

```bash
uv sync --frozen
uv run --no-sync pytest tests/test_vc_champion_apply.py::test_package_install_exposes_shared_gate_without_importing_backend_app -q
uv run --no-sync pytest --ignore=tests/integration -q
```

`--no-sync` intentionally does not refresh an already installed distribution.
If editable metadata predates the entry-point declaration, rerun
`uv sync --frozen --reinstall-package genie-space-optimizer` before testing.
Do not fabricate metadata in pytest setup. The discovery test uses an isolated
Python subprocess, blocks backend/FastAPI imports, and invokes the injected
champion adapter through the installed entry point.

From the repository root, also run:

```bash
python -m pytest backend/tests/test_vc_optimizer_adapter.py -q
```

Verified September 7, 2026: **893 package offline tests passed**, **7 backend
adapter tests passed**. A freshly built wheel installed without dependencies
into a separate temporary environment passed the same isolated entry-point
discovery and champion-adapter invocation probe. Offline collection contained
893 tests and no platform probes; explicit integration collection selected all
six platform probes, while default selection deselected all six.

## Real-platform deployment BLOCKER

`tests/integration/test_vc_optimizer_platform.py` is **not platform-validated**.
Its six Jobs/Genie/Delta probes remain a deployment blocker until M04/M06/M08
target-local composition and disposable fixtures are deployed and exercised.
Offline success and collection-only checks do not satisfy this blocker.

The module carries `pytest.mark.integration`; package defaults deselect it.
The offline command above excludes the entire integration directory from
collection. Explicit integration selection is required:

```bash
uv run --no-sync pytest -m integration tests/integration/test_vc_optimizer_platform.py --collect-only -q
VC_M10_INTEGRATION_CONFIG=/absolute/path/to/disposable-fixtures.json \
  uv run --no-sync pytest -m integration tests/integration/test_vc_optimizer_platform.py -q
```

The JSON manifest must supply `disposable_nonproduction: true`, an explicitly
chosen non-`DEFAULT` `profile`, `host`, `workspace_id`, `catalog`, `control_schema`,
`warehouse_id`, and `executor_principal`. No profile is automatically selected.
Its `cases` map must contain `candidate_abandon`, `champion`, `external_preimage`,
`noop`, `ambiguous`, and `restore`, each with a fresh `run_id`, `operation_id`,
`managed_space_id`, deployed `job_id`, and `parameters` carrying that `run_id`.
Candidate abandonment also needs `candidate_space_id`; champion verification
needs `champion_id`; champion and ambiguous retries need `retry_parameters`
carrying the same `run_id`; restore needs `historical_version_id`.
Jobs must be tagged `vc_test_environment=disposable`, `vc_module=m10`, and run as
the manifest's service principal. Fixture preparation must establish the
scenario-specific initial state, approvals, and ambiguous-outcome injection.
Missing configuration fails explicitly; it is never converted into a skip or
claimed as a platform pass.
