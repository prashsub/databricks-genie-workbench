# M08 TDD evidence

All commands run from the isolated `vc/m08` worktree. Local tooling uses
`export PATH="$PWD/scripts/version_control/.venv/bin:$PATH"` (pytest 9.0.2).
No live profile was selected and no platform mutations were attempted.

| Task | Meaningful RED | GREEN / deployment status |
| --- | --- | --- |
| t01 | `python -m pytest backend/tests/test_vc_provisioning.py -q`: missing callable resource guard assertion | Same command passes; nested resources and unresolved includes rejected |

Initial missing Python/pytest tooling errors are NOT counted as behavioral RED.
