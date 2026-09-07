import { useEffect, useMemo, useState } from 'react'
import type { VersionControlApi } from '@/lib/version-control-api'
import { createMutationIntent } from '@/lib/version-control-api'
import type { ApprovalInputs, ApprovalRecord, DeploymentReceipt, OperationHandle, ReleaseCommand } from '@/types/version-control'
import { pollOperation } from './restore'
import { ApprovalsPanel } from './approvals'
export function ReceiptView({ receipt }: { receipt: DeploymentReceipt }) {
  const verified = receipt.status === 'confirmed' && Boolean(receipt.post_version_id) && JSON.stringify(receipt.rendered_fingerprints) === JSON.stringify(receipt.observed_fingerprints)
  return <section aria-label="Durable deployment receipt">
    <h3>Durable deployment receipt {receipt.release_id}</h3>
    <p role="status">{verified ? 'Deployment confirmed by receipt evidence' : 'Deployment not verified'} · {receipt.status}</p>
    <dl>{(['intended_fingerprints', 'rendered_fingerprints', 'observed_fingerprints'] as const).map((field, index) => <div key={field}><dt>{['Intended', 'Rendered', 'Observed'][index]}</dt><dd><pre>{JSON.stringify(receipt[field], null, 2)}</pre></dd></div>)}</dl>
    <p>Compensation operation: {receipt.compensation_operation_id ?? 'None recorded'}. A compensation request is not proof of compensation.</p>
    <h4>Receipt evidence and lineage</h4><pre>{JSON.stringify(receipt, null, 2)}</pre>
  </section>
}
export async function pollReceipt(api: VersionControlApi, releaseId: string, wait = () => new Promise<void>(resolve => setTimeout(resolve, 1000)), signal?: AbortSignal): Promise<DeploymentReceipt | OperationHandle> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    signal?.throwIfAborted()
    const receipt = await api.receipt(releaseId)
    if ('release_id' in receipt || attempt === 29) return receipt
    await wait()
  }
  throw new Error('Receipt polling exhausted')
}
function ReceiptPanel({ api, releaseId }: { api: VersionControlApi; releaseId: string }) {
  const [receipt, setReceipt] = useState<DeploymentReceipt | OperationHandle | null>(null)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setError('')
    pollReceipt(api, releaseId, undefined, controller.signal).then(result => { if (!controller.signal.aborted) setReceipt(result) })
      .catch(error => { if (!controller.signal.aborted) setError(String(error)) })
    return () => controller.abort()
  }, [api, releaseId, refresh])
  return <section aria-label="Receipt status">
    {receipt && 'release_id' in receipt ? <ReceiptView receipt={receipt} /> : <p role="status">Receipt pending — deployment is not yet verified.</p>}
    {error && <p role="alert">Receipt unreachable: {error}. Do not repeat promotion.</p>}
    <button onClick={() => setRefresh(value => value + 1)}>Refresh receipt evidence</button>
  </section>
}

export function createPromotionFlow(api: VersionControlApi) {
  let selection: ReleaseCommand | null = null
  let releaseId = ''
  let preflight = false
  let revision = 0
  return {
    configure(next: ReleaseCommand) {
      if (JSON.stringify(next) !== JSON.stringify(selection)) { selection = next; releaseId = ''; preflight = false; revision += 1 }
    },
    async validate(key: string) {
      if (!selection?.target_binding.trim() || !selection.mapping_digest.trim() || !selection.source_version_id || !selection.policy.trim() || selection.source_binding === selection.target_binding) throw new Error('Select an explicit target, immutable mapping, source version and policy.')
      const started = revision
      const release = await api.release(selection, `${key}:release`)
      const handle = await api.validate(release.release_id, `${key}:validate`)
      const operation = await pollOperation(api, handle.operation_id)
      if (revision !== started) throw new Error('Selection changed; preflight is invalid.')
      releaseId = release.release_id
      preflight = operation.status === 'confirmed'
      if (!preflight) throw new Error(`Preflight incomplete: ${operation.status}`)
      return releaseId
    },
    async promote(approval: ApprovalRecord | null, key: string) {
      if (!preflight || !releaseId) throw new Error('A confirmed preflight is required.')
      if (!approval?.valid || !approval.approval_digest || approval.inputs.target_binding !== selection?.target_binding || approval.inputs.source_version_id !== selection.source_version_id || approval.inputs.mapping_digest !== selection.mapping_digest || Date.parse(approval.inputs.expires_at) <= Date.now()) throw new Error('Fresh target-local approval matching the selected inputs is required.')
      const handle = await api.promote(releaseId, { approval_id: approval.approval_id }, key)
      return pollOperation(api, handle.operation_id)
    },
  }
}
export function PromotionPanel({ api, bindingId, sourceVersionId, inputs, disabled }: { api: VersionControlApi; bindingId: string; sourceVersionId: string; inputs: ApprovalInputs; disabled: boolean }) {
  const flow = useMemo(() => createPromotionFlow(api), [api])
  const [target, setTarget] = useState('')
  const [mapping, setMapping] = useState('')
  const [policy, setPolicy] = useState('')
  const [releaseId, setReleaseId] = useState('')
  const [approvalId, setApprovalId] = useState('')
  const [approval, setApproval] = useState<ApprovalRecord | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [intent] = useState(createMutationIntent)
  const selection = { source_binding: bindingId, source_version_id: sourceVersionId, target_binding: target, mapping_digest: mapping, policy }
  const change = (setter: (value: string) => void, value: string) => { setter(value); setReleaseId(''); setApprovalId(''); setApproval(null); setError('') }
  const validate = async () => {
    if (disabled || busy) return
    setBusy(true)
    flow.configure(selection)
    try { setReleaseId(await flow.validate(intent.key({ action: 'validate', selection }))); setMessage('Preflight confirmed. Request target-local approval next.') }
    catch (error) { setError(String(error)) }
    finally { setBusy(false) }
  }
  const promote = async () => {
    if (disabled || busy || !releaseId) return
    setBusy(true)
    flow.configure(selection)
    try { const operation = await flow.promote(approval, intent.key({ action: 'promote', selection, approvalId: approval?.approval_id })); setMessage(`Promotion operation ${operation.operation_id}: ${operation.status}. Read the durable receipt before declaring deployment success.`) }
    catch (error) { setError(String(error)) }
    finally { setBusy(false) }
  }
  return <section aria-label="Promotion" className="space-y-3">
    <h3>Promote reviewed version</h3><p>Source: {bindingId} · Version: {sourceVersionId || 'Select a historical version'}</p>
    <fieldset disabled={busy || disabled}><legend>Explicit target-local selection</legend>
      <label>Target binding<input value={target} onChange={event => change(setTarget, event.target.value)} /></label>
      <label>Immutable mapping digest<input value={mapping} onChange={event => change(setMapping, event.target.value)} /></label>
      <label>Validation policy<input value={policy} onChange={event => change(setPolicy, event.target.value)} /></label>
    </fieldset>
    <button disabled={disabled || busy || !sourceVersionId || !target || !mapping || !policy || Boolean(error) || Boolean(releaseId)} onClick={() => void validate()}>Create release and validate mapping</button>
    {releaseId && <ApprovalsPanel key={releaseId} api={api} approvalId={approvalId} inputs={{ ...inputs, operation_type: 'promotion', source_version_id: sourceVersionId, target_binding: target, mapping_digest: mapping }} disabled={disabled || busy} onApproval={setApprovalId} onRecord={setApproval} />}
    <button disabled={disabled || busy || !releaseId || !approval?.valid || Boolean(error)} onClick={() => void promote()}>Promote with target-local approval</button>
    {busy && <p role="status">Promotion workflow pending…</p>}
    {message && <p role="status">{message}</p>}
    {releaseId && <ReceiptPanel api={api} releaseId={releaseId} />}
    {error && <p role="alert">{error} No automatic command replay.</p>}
  </section>
}
