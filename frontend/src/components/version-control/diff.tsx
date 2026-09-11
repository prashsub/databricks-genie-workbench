import type { SemanticDiff } from '@/types/version-control'
export function SemanticDiffView({ diff }: { diff: SemanticDiff }) {
  if (diff.comparison === 'unknown') return <p role="status">Unknown comparison — evidence is unavailable; not proof of equality.</p>
  const categories = [...new Set(diff.items.map(item => item.category))]
  return <section aria-label="Semantic diff">
    <h3>Semantic diff</h3>
    {diff.comparison === 'different' && !diff.items.length && <p role="status">Comparison incomplete — differences reported but no items returned.</p>}
    {diff.comparison === 'equal' && <p>No changes in this comparison.</p>}
    {categories.map(category => <section key={category} aria-label={category}>
      <h4>{category}</h4>
      {diff.items.filter(item => item.category === category).map((item, index) => <article key={`${item.path}-${index}`}>
        <h5>{item.path} · {item.change}</h5>
        {item.review_required && <p role="note">{category === 'sql' ? 'Manual SQL review required' : 'Manual review required'}</p>}
        <dl><dt>Before</dt><dd><pre>{JSON.stringify(item.before, null, 2)}</pre></dd><dt>After</dt><dd><pre>{JSON.stringify(item.after, null, 2)}</pre></dd></dl>
      </article>)}
    </section>)}
  </section>
}
