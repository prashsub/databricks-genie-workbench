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
