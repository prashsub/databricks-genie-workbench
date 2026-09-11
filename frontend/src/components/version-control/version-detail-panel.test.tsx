import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { VersionDetailPanel } from './version-detail-panel'
import { versionFixture } from './fixtures'
import type { VersionDetail } from '@/types/version-control'

const detail: VersionDetail = {
  ...versionFixture,
  origin: 'optimizer',
  optimizer_run_id: 'run/42',
  champion_id: 'champion-7',
  restored_from_version_id: 'original-1',
  snapshot: { data_sources: [{ table: 'sales' }] },
}

it('detail_panel_shows_origin_fingerprints_lineage_and_snapshot', () => {
  const html = renderToStaticMarkup(<VersionDetailPanel detail={detail} onClose={vi.fn()} />)
  for (const text of ['Optimizer', 'Fingerprints', versionFixture.observed_by, 'original-1', 'champion-7', 'Restorable snapshot', 'data_sources', 'Canonicalizer']) {
    expect(html).toContain(text)
  }
})

it('detail_panel_omits_snapshot_section_when_absent', () => {
  const { snapshot, ...withoutSnapshot } = detail
  void snapshot
  const html = renderToStaticMarkup(<VersionDetailPanel detail={withoutSnapshot} onClose={vi.fn()} />)
  expect(html).not.toContain('Restorable snapshot')
})
