import type { BindingStatus } from '@/types/version-control'
export function OverviewBadge({ status }: { status: BindingStatus }) {
  const labels = { clean: 'Policy mismatch', external_ahead: 'External ahead', desired_ahead: 'Desired ahead', diverged: 'Diverged', unknown: 'Unknown', unreachable: 'Unreachable', applied_unverified: 'Applied, unverified', conflicted: 'Conflicted' }
  let label = labels[status.drift]
  if (status.drift === 'clean') {
    label = !status.heads.approved || !status.heads.deployed ? 'Unknown' : status.heads.approved === status.heads.deployed ? 'In sync' : 'Policy mismatch'
  }
  if (status.quarantined) label = 'Quarantined'
  if (status.stale && label === 'In sync') label = 'Unknown'
  return <div className="rounded-lg border border-border p-3 text-sm">
    <strong>{label}</strong>{status.stale && <span> · Stale</span>}
    <dl>{Object.entries(status.heads).map(([head, version]) => <div key={head}><dt>{head}</dt><dd title={version ?? undefined}>{version?.slice(0, 8) ?? 'Not recorded'}</dd></div>)}</dl>
    <p>Projection as of <time>{status.projection_as_of}</time></p>
    <p>Observed: {status.observed_at ?? 'Unknown'}</p>
  </div>
}
