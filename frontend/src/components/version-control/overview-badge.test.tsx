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

it('badge_distinguishes_partial_unresolved_quarantined_conflicted_and_stale_clean', () => {
  const render = (changes = {}) => renderToStaticMarkup(<OverviewBadge status={{ ...bindingFixture, reasons: [], ...changes }} />)
  const markups = [
    render({ drift: 'diverged', unresolved_operation_id: 'op-1' }),
    render({ drift: 'diverged', unresolved_operation_id: null }),
    render({ quarantined: true, drift: 'conflicted' }),
    render({ drift: 'clean', stale: true, heads: { approved: 'p', deployed: 'p', observed: 'x' } }),
    render({ drift: 'unknown' }),
    render({ drift: 'unreachable' }),
  ]
  expect(new Set(markups).size).toBe(markups.length)
  expect(markups[0]).toContain('op-1')
  expect(markups[2]).toContain('Quarantined')
  expect(markups[2]).toContain('Conflicted')
  expect(markups[3]).not.toContain('In sync')
  expect(render({ reasons: ['dual_authority_detected'] })).toContain('dual_authority_detected')
  for (const [drift, label] of [['external_ahead', 'External ahead'], ['desired_ahead', 'Desired ahead'], ['diverged', 'Diverged']]) expect(render({ drift })).toContain(label)
  expect(render({ drift: 'clean', heads: { observed: 'x', approved: null, deployed: null } })).toContain('Unknown')
  expect(render({ drift: 'clean', heads: { observed: 'x', approved: 'p', deployed: 'p' } })).toContain('In sync')
  expect(render({ drift: 'clean', heads: { observed: 'p', approved: 'p', deployed: 'q' } })).toContain('Policy mismatch')
})
