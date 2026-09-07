# M07 TDD evidence

Interpreter: `/home/sandbox-agent/workspace/databricks-genie-workbench/.venv/bin/python`.
All commands run from the m07 worktree. Each numbered task is committed separately.

| Task | Meaningful RED | GREEN |
|---|---|---|
| 1 | Named extraction test: NotImplementedError from package operation | Package test: 1 passed |
| 2 | Collision/missing upload: DID NOT RAISE, 2 failed | Package tests: 4 passed |
| 3 | Named structured mapping: NotImplementedError | Mapping: 1 passed |
| 4 | SQL golden assertion: source identifier unchanged | Mapping: 2 passed |
| 5 | 16 failures: unsupported SQL accepted, executable fields ignored, override ignored | Mapping: 18 passed |
| 6 | 3 failures: missing target environment, stale binding and injected warehouse accepted | Mapping: 21 passed |
| 7 | After correcting fixture payload, 5 NotImplementedError failures in named test | Promotion: 5 passed |
| 8 | Named recomputation test: 8 NotImplementedError failures | Promotion: 13 passed; source bytes also re-canonicalized |
| 9 | 5 DID NOT RAISE failures: source approval, host/workspace, run_as, DEFAULT | Promotion: 19 passed |
| 10 | 7 DID NOT RAISE failures: missing/revised binding, drift, busy/stale/quarantine/unknown | Promotion: 26 passed |
| 11 | Named polling test: NotImplementedError | Dispatch: 1 passed |
| 12 | 4 retry assertions failed; Job stub raised NotImplementedError | Dispatch: 6 passed |
| 13 | Named receipt test returned OperationResult, not DeploymentReceipt | Promotion + dispatch: 33 passed |
| 14 | Export outage test: DID NOT RAISE (export absent) | Promotion + dispatch: 34 passed; repair skips source reads/tests/PATCH |
| 15 | Authorized/failed-restore cases: guarded compensation was never called | Promotion + dispatch: 41 passed; drift/ambiguity/unresolved/late approval never compensate |
| 16 | 8 failures: transport stub, unverified transport/prefix isolation/default source accepted | All M07 offline tests: 74 passed |
| 17 (BLOCKED draft) | Named real-platform test fails explicitly: missing VC_TWO_WORKSPACE_CONFIG; route/DDL/registration/validation tests also observed meaningful RED | Four added offline tests green; integration collectable, NOT passed; implementation stopped at upstream blockers |

## Mandatory ownership stop — September 7, 2026

Tasks 1–16 have individual red/green commits. Task 17 is a preserved draft, not
completed acceptance. No feature switch has been enabled by default.

Read-only executable probes against the integrated upstream implementations found:

1. `platform/identity.py` produces OAuth M2M `ExecutorContext.actor_kind` equal to
   `service_principal`. `mutation_gate.py:149` requires exactly `service` for
   promotion Jobs. Passing the M08-shaped executor to M04 raises
   `PermissionError: Governed writes disabled outside target-local Jobs`.
2. `MutationGate.execute` treats **any** nonempty `OperationFacts.lookup_request`
   history as a retry. M06 correctly returns durable initial request, approval,
   and preflight facts. A requested-only history therefore returns
   `applied_unverified`, unresolved=true, with zero reservations and zero PATCHes.
   A newly approved release cannot reach initial admission through these ports.

These need owner-reviewed M04/M08 integration changes outside M07's manifest.
Per the user's STOP instruction, implementation stopped rather than altering
shared code or hiding authoritative facts behind a promotion-only adapter.
Task 17's release router, command adapters, DDL and live acceptance harness are
preserved for review; they are not a claim of working deployed integration.

The real-platform test requires explicit source, target, approval, source-owner,
and target-owner OAuth M2M profiles, disposable resources, a pre-enrolled binding,
an immutable preapproved operation, native sharing, and an integrated target Job.
It is marked `integration`, deselected by the existing offline pytest default,
and collectable with `-m integration`. No sandbox credentials or deployment
results were fabricated. The missing second workspace remains a separate
deployment blocker even after the code seams are repaired.

## Final offline gate at ownership stop

`/home/sandbox-agent/workspace/databricks-genie-workbench/.venv/bin/python -m pytest --ignore=backend/tests/integration -q`

Result: **1153 passed in 55.71s**. The real two-workspace test is not included.
`git diff --check` passed. All changes are within the exclusive M07 manifest.
