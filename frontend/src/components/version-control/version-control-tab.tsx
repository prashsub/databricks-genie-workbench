import { useVersionControl, canMutate } from '@/hooks/use-version-control'
import type { VersionControlState } from '@/hooks/use-version-control'
import type { VersionControlApi } from '@/lib/version-control-api'
import { OverviewBadge } from './overview-badge'
import { useState } from 'react'
import type { ApprovalRecord, SemanticDiff, VersionSummary } from '@/types/version-control'
import { History } from './history'
import { SemanticDiffView } from './diff'
import { demoApi } from './demo-api'
import { RestorePanel } from './restore'
import { ReconcilePanel } from './reconcile'
import { ApprovalsPanel } from './approvals'
import { approvalInputsFixture } from './fixtures'
import { PromotionPanel } from './promotion'

export function VersionControlView({ state, api }: { state: VersionControlState; api?: VersionControlApi }) {
  return <section aria-label="Version Control and Promotion" aria-live="polite" className="space-y-4">
    <h2>Version Control and Promotion</h2>
    {state.loading && <p role="status">Capturing external history…</p>}
    {state.stale && <p role="alert">Stale history — mutating actions disabled. {state.error}</p>}
    {state.busy && <p role="status">Capture busy — wait for verification. No mutation retry.</p>}
    {!state.loading && !state.history.items.length && <p>No captured versions available.</p>}
    {state.status && <OverviewBadge api={api} status={state.status} />}
    <ul>{state.history.items.map(version => <li key={version.version_id}>{version.version_id} · {version.origin}</li>)}</ul>
    {canMutate(state, 'adopt') && <p>Reconciliation choices are available for the captured base.</p>}
  </section>
}
export function VersionControlTab({ bindingId, api = demoApi }: { bindingId: string; api?: VersionControlApi }) {
  const state = useVersionControl(bindingId, api)
  const [selected, setSelected] = useState<VersionSummary | null>(null)
  const [diff, setDiff] = useState<SemanticDiff | null>(null)
  const [comparing, setComparing] = useState(false)
  const [error, setError] = useState('')
  const [approvalId, setApprovalId] = useState('')
  const [approval, setApproval] = useState<ApprovalRecord | null>(null)
  const base = state.history.items.find(version => version.version_id === state.status?.heads.observed)
  const inputs = { ...approvalInputsFixture, target_binding: bindingId, source_version_id: base?.version_id ?? '', expected_base_fingerprints: base?.fingerprints ?? approvalInputsFixture.expected_base_fingerprints }
  const compare = async (left: string, right: string) => {
    setComparing(true)
    setError('')
    try { setDiff(await api.diff(bindingId, left, right)) }
    catch (error) { setError(String(error)); setDiff({ comparison: 'unknown', items: [] }) }
    finally { setComparing(false) }
  }
  return <div className="space-y-6">
    <VersionControlView api={api} state={state} />
    <button disabled={state.loading} onClick={() => void state.refresh('history')}>Refresh captured history</button>
    <History page={state.history} loading={state.loading} onNext={() => void state.nextPage()} onSelect={setSelected} onCompare={(left, right) => void compare(left, right)} />
    {selected && <p>Selected historical version: {selected.version_id}</p>}
    {selected && state.status && state.history.items.find(version => version.version_id === state.status?.heads.observed) && <RestorePanel
      key={`${bindingId}-${selected.version_id}-${state.status.binding_revision}`} api={api} bindingId={bindingId}
      command={{ version_id: selected.version_id, binding_revision: state.status.binding_revision, expected_base: state.history.items.find(version => version.version_id === state.status?.heads.observed)!.fingerprints }}
      disabled={!canMutate(state, 'restore')} onComplete={() => void state.refresh()} />}
    {comparing && <p role="status">Loading comparison…</p>}
    {error && <p role="alert">{error}</p>}
    {diff && <SemanticDiffView diff={diff} />}
    {base && <ReconcilePanel api={api} state={state} inputs={inputs} approval={approval} onApproval={setApprovalId} onComplete={() => void state.refresh()} onCompare={() => { if (state.status?.heads.approved && state.status.heads.observed) void compare(state.status.heads.approved, state.status.heads.observed) }} />}
    <ApprovalsPanel api={api} approvalId={approvalId} inputs={inputs} disabled={!canMutate(state, 'adopt')} onApproval={setApprovalId} onRecord={setApproval} />
    <PromotionPanel key={`${bindingId}-${selected?.version_id ?? ''}-${state.status?.binding_revision}`} api={api} bindingId={bindingId} sourceVersionId={selected?.version_id ?? ''} inputs={inputs} disabled={!canMutate(state, 'promote')} />
  </div>
}
