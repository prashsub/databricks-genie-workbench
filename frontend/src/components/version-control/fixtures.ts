import type { BindingStatus, Fingerprints, VersionSummary } from '@/types/version-control'

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
