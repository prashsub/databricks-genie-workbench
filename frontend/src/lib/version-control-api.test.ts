import { expect, it, vi } from 'vitest'
import { createMutationIntent, VersionControlApi } from './version-control-api'
import { fingerprints } from '@/components/version-control/fixtures'

it('loads_vc_contract_fixtures_without_type_or_nullability_drift', async () => {
  const fixture = {
    items: [{ binding_id: 'binding/1', binding_revision: 7,
      heads: { observed: 'external-3', approved: 'approved-2', deployed: null },
      drift: 'external_ahead', quarantined: false, unresolved_operation_id: null,
      observed_at: null, projection_as_of: '2026-09-07T00:00:00Z', stale: true,
      allowed_actions: [], reasons: ['Evidence unavailable'] }], next_cursor: null,
  }
  const transport = vi.fn(async () => new Response(JSON.stringify(fixture)))
  const api = new VersionControlApi(transport)
  expect(await api.overview()).toEqual(fixture)
  expect(transport.mock.calls[0]).toEqual(['/api/version-control/overview?limit=25', { signal: undefined }])
  await api.observe('binding/1', 'open', 'intent-1')
  expect(transport.mock.calls[1]).toEqual(['/api/version-control/bindings/binding%2F1/observe', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'intent-1' },
    body: JSON.stringify({ reason: 'open' }),
  }])
})

it('double_click_or_network_loss_does_not_generate_new_mutation_key', async () => {
  const intent = createMutationIntent()
  const body = { version_id: 'version-1', expected_base: fingerprints, binding_revision: 3, approval_id: 'approval-1' }
  const key = intent.key({ bindingId: 'binding-1', body })
  expect(intent.key({ bindingId: 'binding-1', body })).toBe(key)
  const transport = vi.fn().mockRejectedValue(new Error('Network lost after admission'))
  const api = new VersionControlApi(transport)
  const first = api.restore('binding-1', body, key)
  const second = api.restore('binding-1', body, key)
  await expect(first).rejects.toThrow('Network lost')
  await expect(second).rejects.toThrow('Network lost')
  await expect(api.restore('binding-1', body, key)).rejects.toThrow('Network lost')
  expect(transport).toHaveBeenCalledTimes(1)
  const changed = { ...body, version_id: 'version-2' }
  const newKey = intent.key({ bindingId: 'binding-1', body: changed })
  expect(newKey).not.toBe(key)
  await expect(api.restore('binding-1', changed, key)).rejects.toThrow('different intent')
  await expect(api.restore('binding-1', changed, newKey)).rejects.toThrow('Network lost')
  expect(transport).toHaveBeenCalledTimes(2)
})

it('interleaved_intents_reuse_the_original_key_when_the_first_intent_returns', async () => {
  const intent = createMutationIntent()
  const a = { version_id: 'a', expected_base: fingerprints, binding_revision: 3, approval_id: 'approval-1' }
  const b = { ...a, version_id: 'b' }
  const k1 = intent.key(a); const k2 = intent.key(b)
  expect(intent.key(a)).toBe(k1)
  expect(k2).not.toBe(k1)
  const transport = vi.fn(async () => new Response(JSON.stringify({ operation_id: 'operation', status: 'requested', job_run_id: null })))
  const api = new VersionControlApi(transport)
  await api.restore('binding-1', a, k1)
  await api.restore('binding-1', b, k2)
  await api.restore('binding-1', a, intent.key(a))
  expect(transport).toHaveBeenCalledTimes(2)
  await api.restore('binding-1', a, 'mismanaged-new-key')
  expect(transport).toHaveBeenCalledTimes(2)
  await expect(api.restore('binding-1', b, 'mismanaged-new-key')).rejects.toThrow('different intent')
})
