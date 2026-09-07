import { expect, it, vi } from 'vitest'
import { reconcileAction } from './reconcile'
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
