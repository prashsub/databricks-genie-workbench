import type { ApprovalInputs, ApprovalRecord, BindingStatus, DeploymentReceipt, Fingerprints, VersionSummary } from '@/types/version-control'

export const fingerprints: Fingerprints = { config: 'config-3', benchmark: 'benchmark-1', metadata: 'metadata-2', canonicalizer_version: 'VC/1.0' }
export const bindingFixture: BindingStatus = {
  binding_id: 'demo-binding', binding_revision: 3,
  heads: { observed: 'external-3', approved: 'approved-2', deployed: 'deployed-1' },
  drift: 'diverged', quarantined: false, unresolved_operation_id: null,
  observed_at: '2026-09-07T00:00:00Z', projection_as_of: '2026-09-07T00:00:00Z', stale: false,
  allowed_actions: ['restore', 'adopt', 'reapply', 'acknowledge', 'promote'], reasons: ['External changes captured; approval required'],
}
export const versionFixture: VersionSummary = {
  version_id: 'external-3', binding_id: 'demo-binding', observed_at: '2026-09-07T00:00:00Z', origin: 'external',
  observed_by: 'capturing-user', parent_version_id: 'approved-2', restored_from_version_id: null,
  fingerprints, optimizer_run_id: null, champion_id: null,
}
export const approvalInputsFixture: ApprovalInputs = {
  schema_version: 'VC/1.0', operation_id: 'demo-operation', operation_type: 'adopt', source_version_id: 'external-3',
  raw_source_digest: 'raw-3', source_fingerprints: fingerprints, rendered_target_digest: 'rendered-3',
  canonicalizer_version: 'VC/1.0', target_binding: 'demo-binding', expected_base_fingerprints: fingerprints,
  validation_policy_digest: 'validation-1', benchmark_policy_digest: 'benchmark-policy-1', preflight_evidence_digest: 'preflight-1',
  thresholds: { minimum_accuracy: 0.9 }, requester_id: 'demo-requester', recovery_policy: 'quarantine', expires_at: '2026-09-07T23:00:00Z',
}
export const approvalFixture: ApprovalRecord = {
  approval_id: 'demo-approval', inputs: approvalInputsFixture, approval_digest: 'approval-digest-1', valid: false,
  invalidation_reasons: ['Base changed after capture'], allowed_actions: ['vote'], votes: [],
}
export const receiptFixture: DeploymentReceipt = {
  schema_version: 'VC/1.0', release_id: 'demo-release', operation_id: 'demo-promotion', target_binding: 'demo-target',
  attempt_id: 'attempt-1', generation: 1, coordination_backend: 'delta', approval_id: 'demo-approval', approval_digest: 'approval-digest-1',
  source_version_id: 'external-3', package_digest: 'package-1', mapping_digest: 'mapping-1',
  intended_fingerprints: fingerprints, rendered_fingerprints: { ...fingerprints, config: 'rendered-config' },
  observed_fingerprints: { ...fingerprints, config: 'partial-config' }, pre_version_id: 'target-before', post_version_id: null,
  transformer_version: 'transformer-1', canonicalizer_version: 'VC/1.0', executor_id: 'target-executor', job_run_id: 'target-job',
  validation_evidence_digest: 'validation-evidence', benchmark_evidence_digest: 'benchmark-evidence', status: 'applied_partial',
  compensation_operation_id: 'compensation-1', recorded_at: '2026-09-07T01:00:00Z',
}
