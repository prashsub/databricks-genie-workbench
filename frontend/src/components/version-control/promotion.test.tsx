import { expect, it, vi } from 'vitest'
import { createPromotionFlow, ReceiptView, pollReceipt } from './promotion'
import { VersionControlApi } from '@/lib/version-control-api'
import { approvalFixture, receiptFixture } from './fixtures'
import { renderToStaticMarkup } from 'react-dom/server'

it('promotion_requires_explicit_target_mapping_preflight_and_approval', async () => {
  const transport = vi.fn(async (url: RequestInfo | URL) => new Response(JSON.stringify(String(url).endsWith('/releases')
    ? { release_id: 'release-1', operation_id: 'package-1', status: 'requested', job_run_id: null }
    : { operation_id: 'operation-1', status: String(url).includes('/operations/') ? 'confirmed' : 'requested', job_run_id: null, checkpoints: [], audit: [], receipt_references: [] })))
  const flow = createPromotionFlow(new VersionControlApi(transport))
  await expect(flow.validate('no-target')).rejects.toThrow('explicit target')
  const selection = { source_binding: 'source', source_version_id: 'version-1', target_binding: 'target', mapping_digest: 'mapping-1', policy: 'production' }
  flow.configure(selection)
  await expect(flow.promote(approvalFixture, 'premature')).rejects.toThrow('preflight')
  await flow.validate('validate-intent')
  expect(transport.mock.calls.map(([url]) => url)).toEqual(['/api/version-control/releases', '/api/version-control/releases/release-1/validate', '/api/version-control/operations/operation-1'])
  expect(JSON.parse(String(transport.mock.calls[0][1]?.body))).toEqual(selection)
  await expect(flow.promote(approvalFixture, 'invalid-approval')).rejects.toThrow('approval')
  const valid = { ...approvalFixture, valid: true, inputs: { ...approvalFixture.inputs, target_binding: 'target', source_version_id: 'version-1', mapping_digest: 'mapping-1', expires_at: new Date(Date.now() + 3600000).toISOString() } }
  await flow.promote(valid, 'promote-intent')
  expect(transport.mock.calls[3][0]).toBe('/api/version-control/releases/release-1/promote')
  expect(JSON.parse(String(transport.mock.calls[3][1]?.body))).toEqual({ approval_id: valid.approval_id })
  flow.configure({ ...selection, mapping_digest: 'changed-mapping' })
  await expect(flow.promote(valid, 'stale-preflight')).rejects.toThrow('preflight')
})

it('receipt_shows_intended_rendered_observed_and_compensation_status', async () => {
  const html = renderToStaticMarkup(<ReceiptView receipt={receiptFixture} />)
  for (const text of ['Intended', 'Rendered', 'Observed', 'config-3', 'rendered-config', 'partial-config', 'applied_partial', 'compensation-1', 'target-before', 'target-executor', 'package-1']) expect(html).toContain(text)
  expect(html).not.toContain('Deployment confirmed')
  const transport = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ operation_id: 'pending-1', status: 'requested', job_run_id: null }), { status: 202 })).mockResolvedValueOnce(new Response(JSON.stringify(receiptFixture)))
  expect(await pollReceipt(new VersionControlApi(transport), 'release/1', async () => {})).toEqual(receiptFixture)
  expect(transport.mock.calls.map(([url]) => url)).toEqual(['/api/version-control/releases/release%2F1/receipt', '/api/version-control/releases/release%2F1/receipt'])
  expect(transport.mock.calls.every(([, init]) => !init.method)).toBe(true)
})
