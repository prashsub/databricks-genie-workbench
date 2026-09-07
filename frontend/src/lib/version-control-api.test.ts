import { expect, it, vi } from 'vitest'
import { VersionControlApi } from './version-control-api'

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
