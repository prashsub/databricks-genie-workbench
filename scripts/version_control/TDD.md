# M08 TDD evidence

All commands run from the isolated `vc/m08` worktree. Local tooling uses
`export PATH="$PWD/scripts/version_control/.venv/bin:$PATH"` (pytest 9.0.2).
No live profile was selected and no platform mutations were attempted.

## Resumed cycles

| Task | Meaningful RED | GREEN / deployment status |
| --- | --- | --- |
| t04 | Existing draft test fails: missing `provision` callable | Corrected draft names to VC/1.0; validate complete owner set, digests, idempotence and owner verification before ordered execution/grants. Owner DDL/runner delivery remains a deployment blocker. |

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
