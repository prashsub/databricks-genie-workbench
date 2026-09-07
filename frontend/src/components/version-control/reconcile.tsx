import { useState } from 'react'
import type { VersionControlApi } from '@/lib/version-control-api'
import type { ApprovalInputs, ApprovalRecord, ReviewedCommand } from '@/types/version-control'
import { canMutate } from '@/hooks/use-version-control'
import type { VersionControlState } from '@/hooks/use-version-control'
import { pollOperation } from './restore'

export async function reconcileAction(api: VersionControlApi, bindingId: string, action: 'adopt' | 'reapply' | 'acknowledge', reviewed: ReviewedCommand, inputs: ApprovalInputs, approval: ApprovalRecord | null, key: string) {
  if (action === 'adopt') return api.requestApproval(inputs, key)
  if (action === 'reapply' && (!approval?.valid || approval.approval_id !== reviewed.approval_id || approval.inputs.target_binding !== bindingId || Date.parse(approval.inputs.expires_at) <= Date.now() || JSON.stringify(approval.inputs.expected_base_fingerprints) !== JSON.stringify(reviewed.expected_base))) {
    throw new Error('Reapply requires a fresh approval for the newly captured base.')
  }
  const handle = await api.reconcile(bindingId, { ...reviewed, action, policy_inputs: { validation_policy_digest: inputs.validation_policy_digest, benchmark_policy_digest: inputs.benchmark_policy_digest } }, key)
  return pollOperation(api, handle.operation_id)
}
export function ReconcilePanel({ api, state, inputs, approval, onApproval, onComplete, onCompare }: {
  api: VersionControlApi; state: VersionControlState; inputs: ApprovalInputs; approval: ApprovalRecord | null
  onApproval: (approvalId: string) => void; onComplete: () => void; onCompare: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const action = async (action: 'adopt' | 'reapply' | 'acknowledge') => {
    if (!canMutate(state, action) || busy || !state.status) return
    setBusy(true)
    try {
      const result = await reconcileAction(api, state.bindingId, action, { binding_revision: state.status.binding_revision, expected_base: inputs.expected_base_fingerprints, approval_id: approval?.approval_id ?? '' }, inputs, approval, crypto.randomUUID())
      if ('approval_id' in result) { onApproval(result.approval_id); setMessage(`Approval requested: ${result.approval_id}. Not yet approved.`) }
      else { setMessage(`Operation ${result.operation_id}: ${result.status}. Acknowledgement does not align heads.`); onComplete() }
    } catch (error) { setError(String(error)) }
    finally { setBusy(false) }
  }
  return <section aria-label="Reconcile captured changes">
    <h3>Reconcile captured external changes</h3>
    <p>Capture is evidence, not approval. Reapply requires approval for the current base.</p>
    <button onClick={onCompare} disabled={!state.status?.heads.approved || !state.status.heads.observed}>Compare approved with observed</button>
    {(['adopt', 'reapply', 'acknowledge'] as const).map(kind => <button key={kind} disabled={busy || Boolean(error) || !canMutate(state, kind) || (kind === 'reapply' && !approval?.valid)} onClick={() => void action(kind)}>{kind === 'adopt' ? 'Request adoption approval' : kind === 'reapply' ? 'Reapply approved state' : 'Acknowledge scoped divergence'}</button>)}
    {busy && <p role="status">Reconciliation pending…</p>}
    {message && <p role="status">{message}</p>}
    {error && <p role="alert">{error} No automatic mutation retry.</p>}
  </section>
}
