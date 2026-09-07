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
| t07 | Missing storage capability gate assertion | Every missing/false/nonboolean capability and probe outage disables writes; no MODIFY fallback or cached success. |
| t08 | Missing verify-only startup adapter assertion | Wrong/missing run_as or unavailable identity aborts startup before initialization; removed startup Job mutation and swallowed verification failures. |
| t09 | Missing explicit identity provider assertion | Explicit injected OAuth M2M or request-bound OBO clients; reject default/unbound profile and mismatched HTTPS host/workspace/live principal. SQL/Volume handles require the same verified context; no ambient client construction. |
| t10 | Missing trusted snapshot reader assertion | Server-resolved actor/groups/edit permissions; M06 snapshot authorization precedes target-local reader. Denials/outages/cross-workspace reads never invoke trusted reader or grant viewer edit rights; expired OBO never falls back to SP. Live full-reader binding remains unverified. |
| t11 | Missing local dispatcher/runtime assertions | Nine target-only DAB Job entrypoints, operation-ID-only dispatch with deterministic submission token; mutation retries=0, verification retries=2, unresolved/terminal attempts verify only. Deployer sets GSO run_as; startup never repairs it. Live strict DAB validation and owner-handler/DDL composition remain BLOCKED, not passing. |
| t12 | Missing positive termination provider assertion | Exact attempt/root plus complete trusted child/retry inventory and terminal proofs required; missing/active/future/unavailable proof returns None. Worker heartbeat/lease expiry is not proof. Real Jobs enumeration/supervisor reader injection and verified HTTP maximum lifetime remain blockers; termination alone never clears quarantine. |

The `vc-sandbox` target is opt-in and requires explicit executor, enrollment and
provisioner principals. Its installed `vc-platform` entrypoint refuses by default:
`backend.jobs.build_vc_runtime` has no owner implementations available at this
HEAD. M02/M03/M04/M05/M06/M07/M10 handlers, migration manifest loader and reviewed
runner must be composed there before any live provisioning/run can pass. This
is deliberately not replaced with fake production handlers. DAB App import and
source configuration still require a live cutover; existing app deployment is
preserved. The existing app target now requires `gso_run_as_principal` (passed
by deploy.sh); notebook deployment sets the same identity before Job upsert.

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
