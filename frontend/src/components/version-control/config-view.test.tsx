import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { ConfigView } from './config-view'

const snapshot = {
  data_sources: {
    tables: [
      { identifier: 'main.sales.orders', description: 'Order facts', column_configs: [{ column_name: 'region', description: 'Sales region' }] },
    ],
    metric_views: [],
  },
  instructions: {
    text_instructions: [{ content: ['Revenue = SUM(amount).\n'] }],
    join_specs: [{ id: 'j1', left: { identifier: 'orders' }, right: { identifier: 'customers' }, sql: ['`o`.`cid` = `c`.`id`', '--rt=FROM_ONE--'] }],
    sql_snippets: { filters: [{ id: 'f1', display_name: 'Active only', sql: 'status = 1' }] },
    example_question_sqls: [{ id: 'q1', question: 'Top regions?', sql: 'select region from orders' }],
  },
  title: 'Sales agent',
  weird_unmapped_field: { keep: 'me' },
}

it('config_view_renders_friendly_sections_and_preserves_unknowns', () => {
  const html = renderToStaticMarkup(<ConfigView snapshot={snapshot} />)
  for (const text of [
    'Data sources', 'main.sales.orders', 'region',
    'Instructions', 'Revenue = SUM(amount).',
    'Joins', 'orders ⋈ customers',
    'Filters', 'Active only',
    'Sample SQL', 'Top regions?',
    'Metadata', 'Sales agent',
    'Other', 'weird_unmapped_field',
    'View raw JSON',
  ]) {
    expect(html).toContain(text)
  }
})

it('config_view_renders_array_descriptions_as_plain_text', () => {
  const arraySnapshot = {
    data_sources: { tables: [{ identifier: 'cat.sch.tbl', description: ['Demo source orders for VC promotion gate'] }] },
  }
  const html = renderToStaticMarkup(<ConfigView snapshot={arraySnapshot} />)
  // The structured section shows plain text (no JSON brackets); brackets only remain in
  // the raw-JSON disclosure at the bottom.
  expect(html).toContain('Demo source orders for VC promotion gate')
  expect(html.split('View raw JSON')[0]).not.toContain('["Demo source orders')
})

it('config_view_handles_empty_snapshot_gracefully', () => {
  const html = renderToStaticMarkup(<ConfigView snapshot={{}} />)
  expect(html).toContain('No structured configuration in this snapshot.')
  expect(html).toContain('View raw JSON')
})
