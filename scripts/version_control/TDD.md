# M08 TDD evidence

All commands run from the isolated `vc/m08` worktree. Local tooling uses
`export PATH="$PWD/scripts/version_control/.venv/bin:$PATH"` (pytest 9.0.2).
No live profile was selected and no platform mutations were attempted.

## Resumed cycles

| Task | Meaningful RED | GREEN / deployment status |
| --- | --- | --- |
| t04 | Existing draft test fails: missing `provision` callable | Corrected draft names to VC/1.0; validate complete owner set, digests, idempotence and owner verification before ordered execution/grants. Owner DDL/runner delivery remains a deployment blocker. |
| t05 | Missing effective fact-permission verifier assertion | Offline append-only/nonowner/exact SELECT+INSERT checks pass. Real positive INSERT and denied UPDATE/DELETE/ALTER/DROP/REPLACE probes written and integration-marked: NOT EXECUTED, deployment blocker. |
| t06 | Missing coordination grant verifier assertion | Separate nonowner INSERT enrollment and UPDATE-only executor; serialized queued enrollment and exact Serializable required. Real grant/isolation/Job probes marked integration, NOT EXECUTED. Concurrent enrollment/CAS proof still depends on M02/M03 live suites. |

Integration tests live in the three owned `test_vc_*.py` files (not the plan's
unowned `integration/test_vc_permissions.py`). They import the owned integration
fixture. Default pytest deselects `integration`; opt in with `-m integration`.
Explicit selection without `VC_INTEGRATION_CONFIG` fails, never fake-passes.
Configuration requires disposable nonproduction acknowledgement, explicit role
profiles/hosts/workspace/principal/warehouse IDs, namespace, and owner-authored
SQL probes (including safe, isolated insert fixtures and denied DDL probes).
No M08 SQL migration files are authored. No credentials go in the config/logs.

| Task | Meaningful RED | GREEN / deployment status |
| --- | --- | --- |
| t01 | `python -m pytest backend/tests/test_vc_provisioning.py -q`: missing callable resource guard assertion | Same command passes; nested resources and unresolved includes rejected |

Initial missing Python/pytest tooling errors are NOT counted as behavioral RED.

| t02 | `-k existing_optimizer`: missing deployment inventory assertion | Provisioning suite passes; five real deployment paths accounted for |

## Provisioning transition / UNVERIFIED DEPLOYMENT BLOCKERS

| t03 | composition suite: missing root composition assertion | Lazy Protocol factories resolve once, duplicates/missing ports refuse, default-OFF/master gate pass |

Root DAB owns the existing optimizer Job; the package bundle is standalone,
not an additional Workbench deployment. Shell and notebook installers currently
own the App. Do not import it into DAB state until live app-resource support,
permissions, and state migration have been verified with explicit nonprod
profile/target selection. Preserve app.yaml and existing app deploy behavior in
the meantime. No governed Genie content may enter either bundle. VC Jobs and
owner migrations must be integrated before enabling writes; no owner DDL is
invented here. DAB app cutover remains UNVERIFIED, not a passing offline test.
