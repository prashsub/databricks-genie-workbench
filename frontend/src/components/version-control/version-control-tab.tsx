import { useVersionControl, canMutate } from '@/hooks/use-version-control'
import type { VersionControlState } from '@/hooks/use-version-control'
import type { VersionControlApi } from '@/lib/version-control-api'
import { OverviewBadge } from './overview-badge'
import { useState } from 'react'
import type { SemanticDiff, VersionSummary } from '@/types/version-control'
import { History } from './history'
import { SemanticDiffView } from './diff'
import { demoApi } from './demo-api'

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
export function VersionControlTab({ bindingId, api = demoApi }: { bindingId: string; api?: VersionControlApi }) {
  const state = useVersionControl(bindingId, api)
  const [selected, setSelected] = useState<VersionSummary | null>(null)
  const [diff, setDiff] = useState<SemanticDiff | null>(null)
  const [comparing, setComparing] = useState(false)
  const [error, setError] = useState('')
  const compare = async (left: string, right: string) => {
    setComparing(true)
    setError('')
    try { setDiff(await api.diff(bindingId, left, right)) }
    catch (error) { setError(String(error)); setDiff({ comparison: 'unknown', items: [] }) }
    finally { setComparing(false) }
  }
  return <div className="space-y-6">
    <VersionControlView state={state} />
    <button disabled={state.loading} onClick={() => void state.refresh('history')}>Refresh captured history</button>
    <History page={state.history} loading={state.loading} onNext={() => void state.nextPage()} onSelect={setSelected} onCompare={(left, right) => void compare(left, right)} />
    {selected && <p>Selected historical version: {selected.version_id}</p>}
    {comparing && <p role="status">Loading comparison…</p>}
    {error && <p role="alert">{error}</p>}
    {diff && <SemanticDiffView diff={diff} />}
  </div>
}
