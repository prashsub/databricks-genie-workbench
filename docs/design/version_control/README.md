# Genie Agent Version Control and CI/CD Design

**Status:** Draft
**Last updated:** September 6, 2026

## Purpose

This document proposes an architecture for two related but separate capabilities in Genie Workbench:

1. **Version control of live Genie Agents (formerly Genie Spaces)** — save immutable versions, compare them, restore older versions, and reconcile changes made outside Workbench.
2. **Governed cross-workspace CI/CD** — promote an approved version to another Databricks workspace with validation, approvals, rollback, and audit evidence.

The primary design priority is reliable version control and reconciliation. Cross-workspace deployment should be built only after the system can capture, compare, and safely restore live state in one workspace.

## Executive Decision

Use a hybrid architecture with explicit authority boundaries:

- **Genie API read-back** determines what is actually live.
- **Unity Catalog Delta tables** store durable, append-only version and operation facts.
- **Lakebase** coordinates governed mutations and serves rebuildable current-state projections.
- **Git** stores approved portable artifacts and release manifests, not every observed live state.
- **GitHub Actions** provides pull-request governance, protected-environment approvals, and release orchestration.
- **Target-local Databricks Jobs** perform drift checks, mapping, validation, Genie API updates, authoritative read-back, and tests.
- **Databricks Declarative Automation Bundles (DABs)** provision the Workbench, Jobs, tables, permissions, and environment configuration. DABs do not provide Genie version semantics by themselves.

Git alone cannot meet the version-control requirement because changes made directly through the Databricks UI or Genie API may never enter Git. A DAB can deploy the supporting machinery, but the current repository does not manage Genie Agent content as a first-class bundle resource.

## Repository Foundations

Genie Workbench already has several components that should be generalized rather than replaced:

- `backend/services/config_fingerprint.py` canonicalizes Genie configuration and benchmarks independently and calculates stable fingerprints.
- `backend/services/genie_client.py` can retrieve complete Genie Agent state, including `serialized_space`.
- `backend/routers/auto_optimize.py` has conservative current-version and drift checks that avoid declaring drift when history is incomplete.
- `packages/genie-space-optimizer/` stores optimization snapshots and distinguishes submitted configuration from authoritative post-write observations.
- `databricks.yml` provisions optimization Jobs and supporting deployment resources, but not the complete Genie Agent lifecycle.
- Lakebase is currently optional for some Workbench features and may fall back to in-memory state. That fallback must never authorize version-changing operations.

Existing optimizer history is useful but is not a universal version ledger. It captures optimization-specific baselines, iterations, and champions rather than every Workbench, UI, API, restore, or CI/CD change.

## Requirements

### Version Control

The system must:

- Assign each managed Genie Agent a stable logical identity independent of workspace-specific IDs.
- Save immutable, inspectable API-observed versions.
- Preserve exact restorable state as well as canonical state used for comparisons.
- Produce semantic diffs instead of relying only on raw JSON diffs.
- Restore an arbitrary historical version without deleting or rewriting history.
- Capture state before and after every managed mutation.
- Detect externally made changes before Workbench updates, restores, or deployments.
- Preserve newly observed external state before asking the user to adopt, reapply, or reconcile it.
- Avoid silently overwriting unknown live state.
- Record actors, origins, approvals, operation IDs, validation results, and failure states.

### Governed Cross-Workspace CI/CD

The system must:

- Promote a selected immutable source version rather than an unpinned mutable file.
- Separate portable configuration from workspace-specific bindings.
- Resolve catalog, schema, table, metric-view, warehouse, folder, Space ID, and principal mappings.
- Validate target dependencies and permissions before applying changes.
- Block deployment when unexpected target drift exists.
- Capture the target pre-deployment state.
- Verify the applied configuration through an authoritative API read-back.
- Run smoke and benchmark tests.
- Produce a durable deployment receipt and support guarded compensation.

## Non-Goals and Guarantees

The system can guarantee preservation of:

- Every managed pre-mutation and post-mutation boundary.
- Every distinct external state observed by reconciliation.

It cannot guarantee recovery of every intermediate external edit. For example, if a user makes two direct UI edits between reconciliation runs, only the state visible at the next API read may be recoverable unless Databricks exposes a complete configuration event stream.

Restore and deployment are verified compensating operations, not atomic distributed transactions. A Lakebase lease can coordinate Workbench, optimization, and CI/CD writers, but it cannot prevent a user from editing through the Databricks UI.

## Authority Model

| Plane | Authority | Responsibility |
|---|---|---|
| Live state | Genie API read-back | Proves what configuration is currently applied |
| Observation | Unity Catalog Delta | Durable versions, operations, deployment evidence, and audit facts |
| Approval | Git and GitHub | Approved portable artifacts, reviews, release manifests, and environment approvals |
| Coordination | Lakebase | Idempotency, leases, fencing tokens, operation state, and current-head projections |
| Execution | Databricks Jobs | Workspace-local reconciliation, mapping, validation, mutation, verification, and tests |
| Provisioning | DABs | App, Jobs, schemas, permissions, identities, and environment configuration |

No cached head, Git commit, submitted JSON document, or successful API response should be treated as proof of live state without a follow-up API read.

## Canonical Representation Model

One JSON representation should not serve every purpose.

### Response Envelope

The complete Genie API GET response retained for audit and compatibility. It may contain metadata that is not valid as an update payload.

### Restorable State

The exact parsed `serialized_space` plus governed top-level metadata that can be reapplied to the same physical Genie Agent. Restore uses this representation.

### Canonical State

A stable comparison representation that:

- Removes transient and non-restorable fields.
- Normalizes historical wrapper shapes.
- Concatenates Genie fragment arrays where appropriate.
- Sorts object keys and ID-addressed unordered collections.
- Preserves meaningful order.
- Calculates separate configuration, benchmark, and metadata fingerprints.
- Records the canonicalizer version.

The current fingerprint implementation should be extracted into a reusable versioning service and extended with governed top-level metadata.

### Portable Artifact

An environment-neutral representation generated for adoption or promotion. It excludes source workspace IDs, physical Genie Space IDs, warehouse IDs, and workspace paths.

Suggested Git layout:

```text
genie-spaces/
  finance.revenue_analysis/
    manifest.yaml
    serialized-space.json
    environments/
      dev.yaml
      stage.yaml
      prod.yaml
```

The manifest should include the logical `space_key`, artifact schema version, required data sources, validation policy, benchmark policy, ownership, and artifact digest.

## Minimal Authoritative Data Model

The initial implementation should avoid a large event-sourced schema. Start with two durable Delta tables and a small Lakebase coordination model.

### `genie_space_versions`

Append-only authoritative observations:

| Field | Purpose |
|---|---|
| `version_id` | Immutable version UUID |
| `space_key` | Stable logical Genie Agent identity |
| `workspace_id` | Origin workspace |
| `space_id` | Physical Genie Agent ID in that workspace |
| `environment` | Development, test, stage, or production |
| `observed_at` | Observation timestamp |
| `observation_reason` | Managed write, reconciliation, restore, deployment, or manual refresh |
| `observed_by` | User, application principal, deployment principal, or unknown |
| `origin` | Workbench, UI, API, GSO, CI/CD, restore, or unknown |
| `parent_version_id` | Previous observed or reconciliation-base version |
| `restored_from_version_id` | Historical source when created by restore |
| `operation_id` | Correlated governed operation |
| `api_update_time` | Genie-reported update timestamp |
| `response_envelope_json` | Complete API observation |
| `serialized_space_json` | Exact parsed restorable content |
| `restorable_metadata_json` | Governed updateable metadata |
| `config_fingerprint` | Canonical non-benchmark configuration identity |
| `benchmark_fingerprint` | Canonical benchmark identity |
| `metadata_fingerprint` | Governed top-level metadata identity |
| `canonicalizer_version` | Version of comparison rules |
| `git_commit` | Optional approved Git association |
| `release_id` | Optional promotion release association |

Application and deployment principals should not receive `UPDATE` or `DELETE` privileges on this table.

### `genie_space_operations`

Append-only durable operation transitions:

| Field | Purpose |
|---|---|
| `operation_id` | Stable operation UUID |
| `idempotency_key` | Prevents duplicate governed operations |
| `operation_type` | Update, restore, adopt, deploy, or compensate |
| `space_key` | Logical Genie Agent |
| `target_workspace` | Workspace receiving the operation |
| `requested_base_fingerprint` | Live state the approver expected |
| `desired_artifact_fingerprint` | Approved desired source |
| `mapping_fingerprint` | Exact environment transformation rules |
| `transformer_version` | Rendering implementation version |
| `test_policy_fingerprint` | Approved validation rules |
| `pre_version_id` | Durable pre-operation observation |
| `post_version_id` | Durable post-operation observation |
| `approval_reference` | Pull request, environment approval, or break-glass record |
| `job_run_id` | Databricks Job execution |
| `status` | Current durable saga boundary |
| `evidence_json` | Validation, benchmark, and diagnostic evidence |
| `recorded_at` | Transition timestamp |

Meaningful operation states include:

- `requested`
- `preimage_captured`
- `apply_attempted`
- `applied_unverified`
- `confirmed`
- `conflicted`
- `failed`
- `compensation_attempted`
- `compensated`

Detailed execution logs should remain in Databricks Jobs rather than producing a durable event for every function call.

### Lakebase Coordination

Lakebase should maintain reconstructible operational records:

- Current observed, approved, and deployed heads.
- Advisory leases with monotonically increasing fencing tokens.
- Idempotency claims.
- Current saga phase and linked Job run.
- Pending conflict or approval decisions.
- Fast history indexes and cached semantic diffs where useful.

Lakebase should not become a second scheduler or general workflow engine. Databricks Jobs already supplies queues, retries, run state, timeouts, and execution history.

If Lakebase coordination is unavailable:

- History browsing and direct-to-Delta scheduled observation may continue.
- Restore, deployment, adoption, merge, Workbench mutation, and optimization promotion must stop.
- In-memory fallback must not authorize governed mutations.

## Core Workflows

### Managed Snapshot and Update

1. Create an `operation_id` and idempotency key.
2. Acquire a Lakebase lease and fencing token.
3. Fetch the live Genie Agent through the API.
4. Persist an authoritative pre-operation version if it is not already known.
5. Compare the live fingerprints with the expected base.
6. Stop and classify drift if the base does not match.
7. Validate the desired payload.
8. Confirm the fencing token is still current.
9. Submit the Genie update.
10. Fetch the Genie Agent again.
11. Persist the authoritative post-operation version.
12. Compare observed fingerprints with intended fingerprints.
13. Record a terminal operation transition.
14. Update Lakebase projections and release the lease.

If the update call returns but read-back fails, transition to `applied_unverified`. Retry only the read-back. Do not blindly resubmit the mutation.

### Semantic Diff

The Workbench should present categorized differences such as:

- Data sources added or removed.
- Included columns changed.
- Instructions added, removed, or modified.
- Join specifications changed.
- Filters and parameters changed.
- Example SQL changed.
- Benchmarks added, deleted, or modified.
- Sample questions changed.
- Title, description, or governed metadata changed.
- Environment bindings changed separately from portable content.

Arrays with stable IDs should compare by ID rather than list position. SQL-bearing changes should default to manual review.

### External Drift Reconciliation

Reconcile:

- Before every governed mutation.
- When a user opens version history.
- After returning from the Genie UI.
- On a scheduled Databricks Job.
- Before target deployment.

Use `update_time` as a fetch optimization, not as the authoritative equality check. Periodically perform a full API fetch and fingerprint comparison.

Maintain three projected heads:

- **Observed:** Latest API-observed live version.
- **Approved:** Git-approved desired version.
- **Deployed:** Last verified deployment result.

Suggested drift states:

| State | Meaning |
|---|---|
| `clean` | Observed, approved, and deployed states agree |
| `external_ahead` | Live state changed outside the approved deployment path |
| `desired_ahead` | A release is approved but not yet deployed |
| `diverged` | Live and approved state both changed from the common base |
| `unknown` | Authoritative baseline or history is incomplete |
| `unreachable` | Current live state cannot be fetched |
| `applied_unverified` | A write may have succeeded, but read-back is incomplete |
| `conflicted` | Reconciliation requires explicit human resolution |

When a new external state is found, save it before offering:

- **Adopt:** Generate a portable artifact and Git pull request from the observed version.
- **Reapply:** Reapply the approved or deployed version after confirmation.
- **Compare:** Review semantic differences.
- **Merge manually:** Required for overlapping instructions, joins, SQL, or benchmark changes.
- **Acknowledge:** Record intentional environment-specific divergence where policy permits it.

Do not implement general automatic semantic merging in the first release.

### Historical Restore

Restore creates a new version and never mutates history:

1. Display the historical-to-current semantic diff.
2. Acquire a lease and fencing token.
3. Fetch and durably capture the current live state.
4. Confirm the current fingerprint still matches what the user reviewed.
5. Validate the historical state against the current Genie schema and dependencies.
6. Apply the historical restorable state to the stable physical `space_id`.
7. Fetch authoritative state from the Genie API.
8. Persist the resulting version linked by `restored_from_version_id`.
9. Verify configuration, benchmark, and metadata fingerprints.
10. Run smoke or selected benchmark tests.
11. Mark the operation confirmed, conflicted, or failed.

Never automatically compensate after an ambiguous result if doing so could overwrite an unrelated direct edit. Preserve the observed state and require reconciliation.

## Cross-Workspace Promotion

### GitHub Responsibilities

- Review portable artifacts and semantic changes.
- Enforce CODEOWNERS and branch protection.
- Validate artifact schemas and digests.
- Require protected-environment approval.
- Authenticate using OIDC or workload identity federation rather than long-lived PATs.
- Trigger the target-local deployment Job with an immutable release reference.
- Publish deployment evidence back to the release.

### Target-Local Databricks Job Responsibilities

- Resolve the approved artifact and verify its digest.
- Load environment mappings.
- Fetch target live state and block unexplained drift.
- Capture the target pre-deployment version.
- Validate tables, metric views, warehouse access, folders, and permissions.
- Render the target payload.
- Apply the Genie create or update operation.
- Fetch authoritative target state.
- Run smoke questions and benchmark policy.
- Persist the target version and deployment receipt.
- Perform guarded compensation when post-deployment gates fail.

### Mapping Policy

Do not perform unrestricted text replacement across the entire serialized document.

Use an incremental transformation strategy:

1. Rewrite structured table and metric-view identifiers.
2. Rewrite known catalog and schema fields.
3. Bind warehouse, parent path, physical Space ID, and principals outside portable content.
4. Replace exact qualified identifiers inside supported SQL fields.
5. Add parser-assisted SQL rewriting where reliable.
6. Scan executable fields for unresolved source identifiers.
7. Fail closed on ambiguous or unsupported transformations.
8. Require reviewed explicit overrides when necessary.

The approval must bind the artifact hash, mapping hash, transformer version, target environment, expected target base, and test-policy hash.

## Decision Matrix

Scores prioritize live-state versioning and reconciliation.

| Criterion | GitHub Actions + DABs | Databricks-managed | Hybrid |
|---|---:|---:|---:|
| Live drift detection | 2/5 | 5/5 | 5/5 |
| Safe restore | 2/5 | 4/5 | 5/5 |
| External reconciliation | 2/5 | 5/5 | 5/5 |
| Git review and approvals | 5/5 | 3/5 | 5/5 |
| Cross-workspace promotion | 5/5 | 3/5 | 5/5 |
| Security and least privilege | 4/5 | 5/5 | 5/5 |
| Testing and quality gates | 4/5 | 4/5 | 5/5 |
| Secrets and identity | 4/5 | 5/5 | 5/5 |
| Developer experience | 5/5 | 3/5 | 4/5 |
| Maintenance simplicity | 3/5 | 4/5 | 3/5 |
| Repository fit | 3/5 | 4/5 | 5/5 |

### GitHub Actions and DABs

Strong review and release workflow, but weak live-state authority. Git cannot detect direct UI or API changes without a Databricks-side observation service. Adding that service effectively creates a hybrid architecture.

### Databricks-Managed Workflows

Strong live-state access, reconciliation, security, and validation. Less effective as the only multi-workspace approval and release-governance plane.

### Hybrid

Best fit when authority boundaries are explicit. Its additional complexity is justified only if authoritative read-back, idempotency, drift checks, and failure recovery are implemented correctly.

## Security Model

- Use separate observer and deployer service principals where practical.
- Grant the observer only the permissions required to retrieve tracked configurations.
- Grant the deployer update access only to managed target Spaces and required workspace folders.
- Continue using OBO identity for user-sensitive interactive operations.
- Use GitHub OIDC or workload federation instead of Databricks PATs.
- Restrict access to raw snapshots because they may contain business logic, SQL, and metadata.
- Ensure snapshots contain no credentials or secrets.
- Record whether an actor was a human OBO user, Workbench service principal, optimization principal, or deployment principal.
- Require explicit, audited break-glass approval to override drift gates.

## Key Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Direct UI edit races with restore or deployment | Pre/post reads, lease, fencing token, expected-base checks, and conflict state |
| API normalizes submitted JSON | Treat only API read-back as authoritative |
| Polling misses intermediate changes | Capture all managed boundaries and clearly document the externally observed-state guarantee |
| Lakebase outage | Keep history in Delta and stop governed mutations |
| Ambiguous API result is retried destructively | Retry verification reads only; never blindly reapply |
| Canonicalizer changes create false drift | Version canonicalizers and retain exact raw observations |
| Catalog mapping changes SQL literals or instructions | Structured mapping, exact identifier replacement, validation, and fail-closed behavior |
| CI overwrites legitimate target drift | Require the expected target base fingerprint |
| Git approval does not match rendered deployment | Bind approval to artifact, mapping, transformer, target, and policy hashes |
| Restore target is no longer valid | Validate API schema, dependencies, permissions, and smoke tests before confirmation |
| Lakebase becomes a second workflow engine | Use Jobs for queues, retries, and run execution |
| Initial implementation becomes too broad | Start with two Delta tables and local version-control workflows |

## Phased Implementation Plan

### Phase 1: Trustworthy In-Workspace Versions

- Extract shared canonicalization and fingerprint services.
- Add metadata fingerprinting.
- Create `genie_space_versions`.
- Capture authoritative state before and after Workbench and GSO mutations.
- Add history and semantic comparison APIs.
- Add initial version-history UI.

### Phase 2: Guarded Restore and Coordination

- Create `genie_space_operations`.
- Add Lakebase idempotency, advisory leases, and fencing tokens.
- Remove in-memory fallback from version-changing workflows.
- Implement arbitrary historical restore.
- Add authoritative read-back verification and `applied_unverified` recovery.

### Phase 3: External Drift Reconciliation

- Add a scheduled observation Job.
- Reconcile before managed writes and on user refresh.
- Track observed, approved, and deployed heads.
- Add drift classification and notifications.
- Implement adopt, reapply, compare, and manual resolution workflows.

### Phase 4: Portable Git Releases

- Define artifact and release-manifest schemas.
- Export a selected immutable version.
- Generate semantic pull-request summaries.
- Record Git approval evidence and immutable digests in the operation ledger.

### Phase 5: Governed Cross-Workspace Deployment

- Add a DAB-provisioned target deployment Job.
- Configure GitHub OIDC and protected environments.
- Add environment mapping and validation.
- Add target drift gates and target preimage capture.
- Apply, read back, test, and record deployment evidence.

### Phase 6: Hardening

- Add reconciliation lag, failure, and ambiguity metrics.
- Add bounded concurrency, pagination, backoff, and per-Space failure isolation.
- Add retention policies, recovery drills, and compliance exports.
- Add limited automatic merge only for proven disjoint semantic entities.

## Open Questions

- Does the Genie API expose a reliable conditional-update token or ETag in all supported workspaces?
- Which top-level Space fields are consistently updateable and should be included in `metadata_fingerprint`?
- Can system audit tables reliably identify direct Genie UI/API actors and operations?
- What maximum reconciliation lag is acceptable by environment?
- Should production allow direct UI editing, or should policy restrict it operationally?
- Should raw snapshots be retained indefinitely, encrypted separately, or subject to a shorter retention policy?
- Which benchmark threshold and smoke questions are mandatory for promotion?
- How should ACLs be represented: in the portable artifact, a separate policy artifact, or infrastructure configuration?

## Related Decision Record

See [ADR-001: API-Observed Versioning and Governed Promotion](ADR-001-api-observed-versioning.md).
