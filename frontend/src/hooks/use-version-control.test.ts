import { expect, it, vi } from 'vitest'
import { createVersionControlStore } from './use-version-control'
import { VersionControlApi } from '@/lib/version-control-api'
import { bindingFixture, versionFixture } from '@/components/version-control/fixtures'

it('late_response_for_previous_binding_cannot_replace_current_heads', async () => {
  let resolveOld!: (response: Response) => void
  const transport = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/old/observe')) return new Promise<Response>(resolve => { resolveOld = resolve })
    if (url.endsWith('/observe')) return new Response(JSON.stringify({ status: { ...bindingFixture, binding_id: 'current', heads: { observed: 'current-head', approved: null, deployed: null } }, captured_version: null, busy: false }))
    return new Response(JSON.stringify({ items: [{ ...versionFixture, binding_id: url.includes('/old/') ? 'old' : 'current' }], next_cursor: null }))
  })
  const store = createVersionControlStore(new VersionControlApi(transport))
  const old = store.open('old')
  await store.open('current')
  resolveOld(new Response(JSON.stringify({ status: { ...bindingFixture, binding_id: 'old' }, captured_version: versionFixture, busy: false })))
  await old
  expect(store.getSnapshot().status?.binding_id).toBe('current')
  expect(store.getSnapshot().status?.heads.observed).toBe('current-head')
  expect(store.getSnapshot().history.items[0].binding_id).toBe('current')
  await store.invalidate()
  expect(transport.mock.calls.filter(([url]) => String(url).endsWith('/current/observe'))).toHaveLength(2)
})
