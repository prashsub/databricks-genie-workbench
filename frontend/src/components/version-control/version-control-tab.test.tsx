import { expect, it, vi } from 'vitest'
import { createVersionControlStore, canMutate } from '@/hooks/use-version-control'
import { VersionControlApi } from '@/lib/version-control-api'
import { bindingFixture, versionFixture } from './fixtures'

it('open_waits_for_capture_before_showing_reconcile_choices', async () => {
  let completeCapture!: (response: Response) => void
  const transport = vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).endsWith('/observe')) return new Promise<Response>(resolve => { completeCapture = resolve })
    return new Response(JSON.stringify({ items: [versionFixture], next_cursor: null }))
  })
  const store = createVersionControlStore(new VersionControlApi(transport))
  const opening = store.open('demo-binding', 'open')
  expect(canMutate(store.getSnapshot(), 'adopt')).toBe(false)
  expect(store.getSnapshot().loading).toBe(true)
  completeCapture(new Response(JSON.stringify({ status: bindingFixture, captured_version: versionFixture, busy: false })))
  await opening
  expect(store.getSnapshot().history.items).toEqual([versionFixture])
  expect(canMutate(store.getSnapshot(), 'adopt')).toBe(true)
  expect(store.getSnapshot().status?.heads).toEqual(bindingFixture.heads)
  const returning = store.open('demo-binding', 'return')
  expect(canMutate(store.getSnapshot(), 'adopt')).toBe(false)
  completeCapture(new Response(JSON.stringify({ status: bindingFixture, captured_version: versionFixture, busy: false })))
  await returning
  expect(JSON.parse(String(transport.mock.calls.filter(([url]) => String(url).endsWith('/observe')).length))).toBe(2)
})
