# ADR-001: API-Observed Versioning and Governed Promotion

**Status:** Proposed
**Date:** September 6, 2026

## Context

Databricks Genie Agents are mutable workspace resources. They may be changed through Genie Workbench, Genie Space Optimizer, the Databricks UI, direct APIs, or CI/CD automation.

Git and Databricks Declarative Automation Bundles do not independently observe all live changes. Git contains only committed state, while the current bundle provisions supporting application and Job resources rather than owning the complete Genie configuration lifecycle.

The Genie API may normalize submitted configuration. A successful update request or submitted JSON document therefore does not prove that the same representation is currently live. Authoritative state requires a follow-up Genie API read.

The organization requires two distinct capabilities:

1. Recoverable and inspectable version history for live Genie Agents, including externally observed changes.
2. Governed promotion of approved configurations between workspaces.

## Decision

1. Assign each managed Genie Agent a stable logical `space_key` independent of workspace-specific Genie IDs.
2. Store every managed pre-mutation and post-mutation state, and every externally observed state, as an immutable version in governed Unity Catalog Delta tables.
3. Treat authoritative Genie API read-back as evidence of live applied state.
4. Preserve four representations where appropriate:
   - Complete API response envelope for audit.
   - Exact restorable state for same-workspace recovery.
   - Canonical state and versioned fingerprints for semantic comparison.
   - Portable artifacts for governed cross-workspace promotion.
5. Extend the repository's existing configuration and benchmark fingerprint logic and add governed metadata fingerprinting.
6. Store durable operation and deployment transitions in Unity Catalog Delta.
7. Use Lakebase for rebuildable operational coordination, including idempotency, leases with fencing tokens, current saga state, and current-head projections.
8. Stop restore, deployment, adoption, merge, Workbench mutation, and optimization promotion when durable Lakebase coordination is unavailable. In-memory fallback must not authorize these operations.
9. Use Git for approved portable artifacts and release manifests, not as the sole live-state version history.
10. Maintain observed, approved, and deployed heads separately.
11. Capture and persist unexpected live state before asking a user to adopt, reapply, compare, acknowledge, or manually reconcile it.
12. Use GitHub Actions with OIDC or workload identity federation for review, approvals, and release orchestration.
13. Use DAB-provisioned target-local Databricks Jobs for target drift checks, environment mapping, validation, Genie API mutation, authoritative read-back, tests, and deployment evidence.
14. Use DABs to provision infrastructure and workflows, not as the authoritative Genie Agent content manager.
15. Bind production approval to the portable artifact hash, mapping hash, transformer version, target environment, expected target base, and test-policy hash.
16. Implement restore and deployment as idempotent sagas with explicit ambiguous and conflict states.
17. Treat historical restore as a verified compensating operation, not an atomic rollback.
18. Block governed mutations on unexpected live drift unless an authorized user adopts or explicitly overrides the drift.
19. Use structured and exact-identifier environment mappings. Fail closed on ambiguous transformations rather than globally replacing arbitrary text.
20. Implement reliable in-workspace capture, comparison, reconciliation, and restore before building cross-workspace promotion automation.

## Consequences

### Positive

- Direct UI or API changes are not silently overwritten after they have been observed.
- Every managed mutation has a durable preimage and verified post-operation observation.
- Historical restore preserves lineage rather than rewriting history.
- Git remains reviewable and focused on approved releases.
- Target deployments are verified against actual target state.
- Deployment evidence includes intended, rendered, and observed identities.
- DAB limitations do not constrain the version model.

### Negative

- The system coordinates Genie API operations, Delta, Lakebase, GitHub, and Databricks Jobs without a shared transaction.
- Lakebase becomes operationally required for governed mutations, even though it is not authoritative and can be rebuilt.
- Restore and deployment cannot be described as strictly atomic.
- Scheduled reconciliation cannot recover external intermediate states that are never observed.
- Versioned canonicalization, idempotency, reconciliation, and recovery add implementation complexity.
- GitHub and Databricks operation evidence must be correlated and retained.

### Neutral

- Direct UI editing remains technically possible unless separate organizational policy restricts it.
- Existing optimizer history remains optimization-specific and should not become the universal version ledger.
- General automatic semantic merging is deferred. SQL-bearing and overlapping changes require human review.

## Alternatives Considered

### GitHub Actions and DABs as the Complete Solution

Rejected as the sole version-control architecture. It provides strong review and release governance but cannot observe out-of-band live changes without an additional workspace-side reconciler. A Git revert also does not prove that a live Genie Agent was safely restored.

### Databricks-Managed Workflows Only

Rejected as the complete governance architecture. It provides strong proximity to live state, identities, data dependencies, and tests, but is less effective for Git-based review, immutable release selection, protected-environment approval, and multi-workspace orchestration.

### Hybrid Architecture

Accepted. Databricks owns observation, validation, mutation, and verification close to the live resource. GitHub owns reviewed desired state and release approval. Authority boundaries prevent either system from incorrectly claiming complete ownership.

## Implementation Constraints

- Start with `genie_space_versions` and `genie_space_operations`; add specialized ledgers only when needed.
- Use Databricks Jobs rather than Lakebase as the execution queue and retry engine.
- Fetch live state before every governed write even when a current-head projection exists.
- Persist the pre-operation observation before submitting a mutation.
- When a write returns but read-back fails, retry verification only and block further governed mutation until reconciled.
- Do not implement a universal SQL rewrite engine in the first deployment release.
- Do not use unrestricted string replacement across `serialized_space`.
- Do not claim that every transient direct UI edit can be recovered.

## Follow-Up Decisions

- Snapshot retention and access policy.
- ACL representation and promotion strategy.
- Reconciliation schedules and service-level objectives.
- Production direct-edit policy.
- Canonicalizer migration policy.
- Benchmark and smoke-test promotion thresholds.
