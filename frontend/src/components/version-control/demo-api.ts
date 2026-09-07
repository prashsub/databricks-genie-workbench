import { VersionControlApi } from '@/lib/version-control-api'
import { bindingFixture, versionFixture } from './fixtures'

export const demoTransport: typeof fetch = async (input, init) => {
  const url = new URL(String(input), 'https://vc-demo.invalid')
  const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
  if (init?.method === 'POST' && url.pathname.endsWith('/observe')) return json({ status: bindingFixture, captured_version: versionFixture, busy: false })
  if (url.pathname.endsWith('/overview')) return json({ items: [bindingFixture], next_cursor: null })
  if (url.pathname.endsWith('/status')) return json(bindingFixture)
  if (url.pathname.endsWith('/versions')) return json({ items: [versionFixture], next_cursor: null })
  if (url.pathname.endsWith('/diff')) return json({ comparison: 'known', items: [{ category: 'sql', path: 'example_queries/1', change: 'modified', before: 'SELECT old_column FROM sales', after: 'SELECT new_column FROM sales', review_required: true }] })
  return json({ code: 'DEMO_NOT_IMPLEMENTED', message: 'This fixture route is not available.', stale: true, retryable: false }, 404)
}
export const demoApi = new VersionControlApi(demoTransport)
