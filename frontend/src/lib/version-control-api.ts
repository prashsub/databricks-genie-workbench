import type { ApiError, ApprovalInputs, ApprovalRecord, ApprovalRequest, BindingStatus, DeploymentReceipt, ObservationResult, Operation, OperationHandle, OverviewPage, ReconcileCommand, ReleaseCommand, ReleaseHandle, RestoreCommand, SemanticDiff, VersionDetail, VersionPage } from '@/types/version-control'

export class VersionControlError extends Error implements ApiError {
  code: string
  operation_id?: string
  retryable: boolean
  stale: boolean
  status: number
  constructor(status: number, error: ApiError) {
    super(error.message)
    this.status = status
    this.code = error.code
    this.operation_id = error.operation_id
    this.retryable = error.retryable
    this.stale = error.stale
  }
}

const bindingPath = (bindingId: string) => `/bindings/${encodeURIComponent(bindingId)}`
const pageQuery = (cursor?: string, limit = 25) => {
  const query = new URLSearchParams({ limit: String(Math.min(100, Math.max(1, limit))) })
  if (cursor) query.set('cursor', cursor)
  return query.toString()
}

export class VersionControlApi {
  private transport: typeof fetch
  constructor(transport: typeof fetch) { this.transport = transport }
  private async request<Result>(path: string, init: RequestInit): Promise<Result> {
    const response = await this.transport(`/api/version-control${path}`, init)
    const payload = await response.json()
    if (!response.ok) throw new VersionControlError(response.status, payload)
    return payload as Result
  }
  private get<Result>(path: string, signal?: AbortSignal) { return this.request<Result>(path, { signal }) }
  private post<Result>(path: string, body: unknown, key: string) {
    return this.request<Result>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(body) })
  }
  overview(cursor?: string, signal?: AbortSignal) { return this.get<OverviewPage>(`/overview?${pageQuery(cursor)}`, signal) }
  status(bindingId: string, signal?: AbortSignal) { return this.get<BindingStatus>(`${bindingPath(bindingId)}/status`, signal) }
  versions(bindingId: string, cursor?: string, signal?: AbortSignal) { return this.get<VersionPage>(`${bindingPath(bindingId)}/versions?${pageQuery(cursor)}`, signal) }
  version(bindingId: string, versionId: string) { return this.get<VersionDetail>(`${bindingPath(bindingId)}/versions/${encodeURIComponent(versionId)}`) }
  diff(bindingId: string, left: string, right: string) { return this.get<SemanticDiff>(`${bindingPath(bindingId)}/diff?${new URLSearchParams({ left, right })}`) }
  observe(bindingId: string, reason: 'open' | 'history' | 'refresh' | 'return', key: string) { return this.post<ObservationResult>(`${bindingPath(bindingId)}/observe`, { reason }, key) }
  restore(bindingId: string, body: RestoreCommand, key: string) { return this.post<OperationHandle>(`${bindingPath(bindingId)}/restore`, body, key) }
  reconcile(bindingId: string, body: ReconcileCommand, key: string) { return this.post<OperationHandle>(`${bindingPath(bindingId)}/reconcile`, body, key) }
  requestApproval(body: ApprovalInputs, key: string) { return this.post<ApprovalRequest>('/approvals', body, key) }
  vote(approvalId: string, decision: 'approve' | 'reject', key: string) { return this.post<ApprovalRecord>(`/approvals/${encodeURIComponent(approvalId)}/votes`, { decision }, key) }
  approval(approvalId: string) { return this.get<ApprovalRecord>(`/approvals/${encodeURIComponent(approvalId)}`) }
  operation(operationId: string) { return this.get<Operation>(`/operations/${encodeURIComponent(operationId)}`) }
  release(body: ReleaseCommand, key: string) { return this.post<ReleaseHandle>('/releases', body, key) }
  validate(releaseId: string, key: string) { return this.post<OperationHandle>(`/releases/${encodeURIComponent(releaseId)}/validate`, {}, key) }
  promote(releaseId: string, body: { approval_id: string }, key: string) { return this.post<OperationHandle>(`/releases/${encodeURIComponent(releaseId)}/promote`, body, key) }
  receipt(releaseId: string) { return this.get<DeploymentReceipt | OperationHandle>(`/releases/${encodeURIComponent(releaseId)}/receipt`) }
}
