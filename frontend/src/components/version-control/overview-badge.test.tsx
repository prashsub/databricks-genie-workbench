import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { OverviewBadge } from './overview-badge'
import { bindingFixture } from './fixtures'

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
