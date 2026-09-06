# Debby Handoff: Genie Agent Version Control Debate

**Prepared:** September 6, 2026

## Repository

```text
/home/sandbox-agent/workspace/databricks-genie-workbench
```

Branch:

```text
version-control-cicd
```

## Documents to Review

- `docs/design/version_control/README.md`
- `docs/design/version_control/ADR-001-api-observed-versioning.md`

## Current Proposed Direction

- Use API-observed state rather than Git as the authoritative live version source.
- Store durable versions and operation facts in append-only Unity Catalog Delta tables.
- Use Lakebase for rebuildable coordination, leases, idempotency, and current-head projections.
- Fail governed mutations closed when Lakebase coordination is unavailable.
- Store approved portable releases in Git.
- Use GitHub Actions for review and approval.
- Use target-local Databricks Jobs for validation, deployment, authoritative read-back, and testing.
- Use DABs to provision the machinery rather than pretending Genie content is a native bundle resource.
- Implement local snapshot, diff, restore, and external reconciliation before cross-workspace deployment.

## Suggested Debby Prompt

Use the `debate` skill for 2 rounds.

Read:

- `/home/sandbox-agent/workspace/databricks-genie-workbench/docs/design/version_control/README.md`
- `/home/sandbox-agent/workspace/databricks-genie-workbench/docs/design/version_control/ADR-001-api-observed-versioning.md`
- The repository implementation referenced by those documents.

Critique the proposed architecture for version control and CI/CD of Databricks Genie Agents. Focus primarily on whether the proposed version ledger, canonicalization, restore protocol, and external-drift reconciliation are correct and implementable.

Challenge these assumptions:

1. Unity Catalog Delta should be the authoritative durable version and operation ledger.
2. Lakebase should be non-authoritative but operationally required for governed mutations.
3. Two Delta tables are enough for the first implementation.
4. Every managed pre/post state and every externally observed state is an accurate and sufficient product guarantee.
5. Advisory leases, fencing tokens, expected-base fingerprints, and authoritative read-back are sufficient without a Genie conditional-update API.
6. Git should contain only approved portable artifacts rather than every observed version.
7. DABs should provision reconciliation and deployment Jobs rather than manage Genie content directly.
8. Cross-workspace deployment should be delayed until local capture, comparison, restore, and reconciliation are reliable.

Evaluate repository fit, failure recovery, security boundaries, operational cost, scale, schema evolution, auditability, and developer experience.

Finish with:

- Required changes to the architecture document.
- Required changes to the ADR.
- Any missing repository constraints.
- A right-sized first implementation milestone.
- Acceptance criteria for version capture, drift detection, restore, and reconciliation.
- A list of unresolved Databricks platform/API questions that must be validated before implementation.

## Questions Requiring Further Debate

- Is Lakebase necessary in the MVP, or can idempotency and coordination initially be implemented through Jobs plus Delta without weakening safety?
- Does the Genie API expose an update token, ETag, or other conditional-write mechanism not currently used by Workbench?
- Should the logical Space registry be another durable Delta table immediately, or initially be represented through version rows and Lakebase projections?
- Which fields belong in restorable state versus environment-specific bindings?
- How should generated Genie IDs be matched across versions without producing noisy diffs?
- Can audit logs reliably attribute external Genie changes to a user and operation?
- What reconciliation interval is operationally and financially acceptable?
- Which changes can ever be automatically merged safely?
- Should production direct editing be prohibited by policy even though reconciliation supports it?
- What evidence should a deployment receipt retain for governance and rollback?
