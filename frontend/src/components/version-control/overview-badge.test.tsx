import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { OverviewBadge } from './overview-badge'
import { bindingFixture } from './fixtures'
import { loadOverview } from './overview'
import { VersionControlApi } from '@/lib/version-control-api'

it('overview_badge_uses_approved_deployed_not_observed_equality', () => {
  const render = (changes = {}) => renderToStaticMarkup(<OverviewBadge status={{ ...bindingFixture, ...changes }} />)
  expect(render()).not.toContain('In sync')
  expect(render({ drift: 'clean', heads: { observed: 'same', approved: null, deployed: null } })).toContain('Unknown')
  expect(render({ drift: 'clean', heads: { observed: 'same', approved: 'policy', deployed: 'policy' } })).toContain('In sync')
  for (const [drift, label] of [['unknown', 'Unknown'], ['unreachable', 'Unreachable'], ['applied_unverified', 'Applied, unverified'], ['conflicted', 'Conflicted']]) {
    expect(render({ drift })).toContain(label)
  }
  expect(render({ quarantined: true })).toContain('Quarantined')
  expect(render({ stale: true })).toContain('Stale')
  expect(render()).toContain('external')
  expect(render()).toContain(bindingFixture.projection_as_of)
})

it('overview_does_not_fetch_live_genie_per_row', async () => {
  const page = { items: Array.from({ length: 25 }, (_, index) => ({ ...bindingFixture, binding_id: `binding-${index}` })), next_cursor: 'page/2' }
  const transport = vi.fn(async () => new Response(JSON.stringify(page)))
  const api = new VersionControlApi(transport)
  expect(await loadOverview(api)).toEqual(page)
  expect(transport).toHaveBeenCalledTimes(1)
  await loadOverview(api, page.next_cursor)
  expect(transport.mock.calls[1][0]).toBe('/api/version-control/overview?limit=25&cursor=page%2F2')
  expect(transport.mock.calls.every(([url]) => String(url).includes('/overview?'))).toBe(true)
})
