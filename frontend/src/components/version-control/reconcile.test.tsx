import { expect, it, vi } from 'vitest'
import { reconcileAction, OperationStatusView } from './reconcile'
import { renderToStaticMarkup } from 'react-dom/server'
import { VersionControlApi } from '@/lib/version-control-api'
import { approvalFixture, approvalInputsFixture, fingerprints } from './fixtures'

it('adopt_requests_approval_and_reapply_does_not_reuse_invalid_approval', async () => {
  const transport = vi.fn(async () => new Response(JSON.stringify(approvalFixture)))
  const api = new VersionControlApi(transport)
  const reviewed = { binding_revision: 3, expected_base: fingerprints, approval_id: approvalFixture.approval_id }
  await reconcileAction(api, 'demo-binding', 'adopt', reviewed, approvalInputsFixture, approvalFixture, 'adopt-intent')
  expect(transport.mock.calls[0][0]).toBe('/api/version-control/approvals')
  expect(JSON.parse(String(transport.mock.calls[0][1]?.body))).toEqual(approvalInputsFixture)
  await expect(reconcileAction(api, 'demo-binding', 'reapply', reviewed, approvalInputsFixture, approvalFixture, 'reapply-intent')).rejects.toThrow('fresh approval')
  expect(transport).toHaveBeenCalledTimes(1)
})

it('quarantine_and_partial_outcome_never_offer_blind_retry', () => {
  for (const status of ['quarantined', 'applied_partial', 'applied_unverified', 'conflicted'] as const) {
    const html = renderToStaticMarkup(<OperationStatusView operation={{ operation_id: 'unresolved-1', status, job_run_id: null, checkpoints: ['Description write incomplete'], audit: ['Operator must inspect captured evidence'], receipt_references: [] }} onVerify={vi.fn()} />)
    expect(html).toContain(status)
    expect(html).toContain('Description write incomplete')
    expect(html).toContain('Operator must inspect captured evidence')
    expect(html).toContain('Verify operation status')
    expect(html).not.toMatch(/>Retry|>Reapply|>Restore|>Promote/)
    expect(html).toContain('No mutation replay')
  }
})
