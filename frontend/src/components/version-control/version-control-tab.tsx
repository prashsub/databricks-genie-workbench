import { useVersionControl, canMutate } from '@/hooks/use-version-control'
import type { VersionControlState } from '@/hooks/use-version-control'
import type { VersionControlApi } from '@/lib/version-control-api'
import { OverviewBadge } from './overview-badge'

export function VersionControlView({ state }: { state: VersionControlState }) {
  return <section aria-label="Version Control and Promotion" className="space-y-4">
    <h2>Version Control and Promotion</h2>
    {state.loading && <p role="status">Capturing external history…</p>}
    {state.stale && <p role="alert">Stale history — mutating actions disabled. {state.error}</p>}
    {state.busy && <p role="status">Capture busy — wait for verification. No mutation retry.</p>}
    {!state.loading && !state.history.items.length && <p>No captured versions available.</p>}
    {state.status && <OverviewBadge status={state.status} />}
    <ul>{state.history.items.map(version => <li key={version.version_id}>{version.version_id} · {version.origin}</li>)}</ul>
    {canMutate(state, 'adopt') && <fieldset><legend>Reconcile captured changes</legend><button>Request adoption approval</button></fieldset>}
  </section>
}
export function VersionControlTab({ bindingId, api }: { bindingId: string; api?: VersionControlApi }) {
  const state = useVersionControl(bindingId, api)
  return <VersionControlView state={state} />
}
