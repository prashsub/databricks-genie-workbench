import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { ApprovalDetails, voteApproval } from './approvals'
import { approvalFixture } from './fixtures'
import { VersionControlApi } from '@/lib/version-control-api'

it('approval_ui_shows_bound_digests_distinct_approvers_and_expiry', async () => {
  const vote = { actor_id: 'human-1', decision: 'approve' as const, recorded_at: '2026-09-07T01:00:00Z', target_group_eligible: true }
  const record = { ...approvalFixture, votes: [vote, vote, { ...vote, actor_id: 'demo-requester' }] }
  const html = renderToStaticMarkup(<ApprovalDetails record={record} />)
  for (const text of ['raw-3', 'rendered-3', 'validation-1', 'benchmark-policy-1', 'preflight-1', '1 distinct', '2026-09-07T23:00:00Z', 'Base changed after capture', 'neither requester nor deployer', 'Server authorization']) expect(html).toContain(text)
  const transport = vi.fn(async () => new Response(JSON.stringify(record)))
  await voteApproval(new VersionControlApi(transport), record, 'approve', 'vote-intent')
  expect(transport.mock.calls[0][0]).toBe('/api/version-control/approvals/demo-approval/votes')
  expect(JSON.parse(String(transport.mock.calls[0][1]?.body))).toEqual({ decision: 'approve' })
  await expect(voteApproval(new VersionControlApi(transport), { ...record, allowed_actions: [] }, 'approve', 'denied')).rejects.toThrow('not allowed')
  expect(transport).toHaveBeenCalledTimes(1)
})
