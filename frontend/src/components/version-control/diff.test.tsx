import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { SemanticDiffView } from './diff'

it('semantic_diff_groups_changes_and_flags_manual_sql_review', () => {
  const html = renderToStaticMarkup(<SemanticDiffView diff={{ comparison: 'different', items: [
    { category: 'sql', path: 'queries/1', change: 'modified', before: 'select 1', after: 'select 2', review_required: true },
    { category: 'columns', path: 'sales/id', change: 'added', before: null, after: 'id', review_required: false },
  ] }} />)
  // The SQL value renders as a side-by-side, word-level diff (Before/After columns); the
  // changed token is split into its own span, so assert on the rendered pieces.
  for (const text of ['Sample SQL', 'Columns', 'Manual SQL review required', 'select ', 'Before', 'After', 'word-removed', 'word-added', 'sales/id']) expect(html).toContain(text)
  const unknown = renderToStaticMarkup(<SemanticDiffView diff={{ comparison: 'unknown', items: [] }} />)
  expect(unknown).toContain('Unknown comparison')
  expect(unknown).not.toContain('No changes')
})

it('diff_never_infers_equality_from_an_empty_item_list', () => {
  const render = (comparison: 'equal' | 'different' | 'unknown') => renderToStaticMarkup(<SemanticDiffView diff={{ comparison, items: [] }} />)
  expect(render('different')).not.toContain('No changes')
  expect(render('different')).toContain('Comparison incomplete')
  expect(render('equal')).toContain('No changes')
  expect(render('unknown')).toContain('Unknown comparison')
  expect(render('unknown')).not.toContain('No changes')
})
