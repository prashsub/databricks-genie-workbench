import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { History } from './history'
import { versionFixture } from './fixtures'

it('history_shows_origin_actor_lineage_and_optimizer_drillthrough', () => {
  const html = renderToStaticMarkup(<History page={{ items: [{ ...versionFixture, origin: 'optimizer', optimizer_run_id: 'run/42', champion_id: 'champion-7', restored_from_version_id: 'original-1' }], next_cursor: 'next' }} onNext={vi.fn()} onSelect={vi.fn()} onCompare={vi.fn()} />)
  for (const text of ['optimizer', versionFixture.observed_by, 'approved-2', 'original-1', 'champion-7', 'run%2F42', 'Compare versions', 'Next history page', 'external-3']) expect(html).toContain(text)
  expect(html).not.toContain('Version 3')
})
