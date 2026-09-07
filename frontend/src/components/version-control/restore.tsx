import { useState } from 'react'
import { VersionControlError } from '@/lib/version-control-api'
import type { VersionControlApi } from '@/lib/version-control-api'
import type { Operation, RestoreCommand } from '@/types/version-control'

export async function pollOperation(api: VersionControlApi, operationId: string, onUpdate?: (operation: Operation) => void) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const operation = await api.operation(operationId)
    onUpdate?.(operation)
    if (!['requested', 'preimage_captured', 'apply_attempted', 'compensation_attempted'].includes(operation.status) || attempt === 29) return operation
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  throw new Error('Operation polling exhausted')
}
export async function submitRestore(api: VersionControlApi, bindingId: string, command: RestoreCommand, confirmed: boolean, key: string, onUpdate?: (operation: Operation) => void) {
  if (!confirmed || !command.approval_id.trim()) throw new Error('Confirm the reviewed version and supply a fresh approval reference.')
  try {
    const handle = await api.restore(bindingId, command, key)
    return await pollOperation(api, handle.operation_id, onUpdate)
  } catch (error) {
    if (error instanceof VersionControlError && error.operation_id) return pollOperation(api, error.operation_id, onUpdate)
    throw error
  }
}
export function RestorePanel({ api, bindingId, command, disabled, onComplete }: { api: VersionControlApi; bindingId: string; command: Omit<RestoreCommand, 'approval_id'>; disabled: boolean; onComplete: () => void }) {
  const [approvalId, setApprovalId] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [error, setError] = useState('')
  const submit = async () => {
    if (disabled || busy) return
    setBusy(true)
    setError('')
    try { setOperation(await submitRestore(api, bindingId, { ...command, approval_id: approvalId }, confirmed, crypto.randomUUID(), setOperation)); onComplete() }
    catch (error) { setError(String(error)) }
    finally { setBusy(false) }
  }
  return <section aria-label="Restore or roll forward">
    <h3>Restore / roll forward</h3>
    <p>Use historical content as a new reviewed write; history is never rewound.</p>
    <p>Version {command.version_id} · Reviewed revision {command.binding_revision}</p>
    <pre>{JSON.stringify(command.expected_base, null, 2)}</pre>
    <label>Restore approval reference<input value={approvalId} onChange={event => setApprovalId(event.target.value)} /></label>
    <label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />Confirm reviewed version and base</label>
    <button disabled={disabled || busy || !confirmed || !approvalId.trim() || Boolean(error) || Boolean(operation)} onClick={() => void submit()}>Submit restore / roll forward</button>
    {busy && <p role="status">Operation pending — polling status, not replaying the command.</p>}
    {operation && <p role="status">Operation {operation.operation_id}: {operation.status}</p>}
    {error && <p role="alert">{error} Command outcome unresolved; do not blindly retry.</p>}
  </section>
}
