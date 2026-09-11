import { Badge } from '@/components/ui/badge'
import type { BadgeProps } from '@/components/ui/badge'
import type { SemanticDiff } from '@/types/version-control'

type DiffChangeValue = 'added' | 'removed' | 'modified'

const CHANGE_VARIANT: Record<DiffChangeValue, BadgeProps['variant']> = {
  added: 'success',
  removed: 'danger',
  modified: 'warning',
}

export function SemanticDiffView({ diff }: { diff: SemanticDiff }) {
  if (diff.comparison === 'unknown') {
    return (
      <section aria-label="Semantic diff" className="rounded-xl border border-default bg-surface p-4">
        <p role="status" className="text-sm text-muted">
          Unknown comparison — evidence is unavailable; not proof of equality.
        </p>
      </section>
    )
  }

  const categories = [...new Set(diff.items.map(item => item.category))]
  return (
    <section aria-label="Semantic diff" className="rounded-xl border border-default bg-surface p-4 space-y-4">
      <h3 className="text-sm font-semibold text-secondary uppercase tracking-wide">Semantic diff</h3>

      {diff.comparison === 'different' && !diff.items.length && (
        <p role="status" className="text-sm text-amber-400">
          Comparison incomplete — differences reported but no items returned.
        </p>
      )}
      {diff.comparison === 'equal' && (
        <p className="text-sm text-muted">No changes in this comparison.</p>
      )}

      {categories.map(category => (
        <section key={category} aria-label={category} className="space-y-2">
          <h4 className="text-xs font-semibold text-secondary uppercase tracking-wide">{category}</h4>
          {diff.items.filter(item => item.category === category).map((item, index) => (
            <article key={`${item.path}-${index}`} className="rounded-lg border border-default bg-surface-secondary/40 p-3 space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                <Badge variant={CHANGE_VARIANT[item.change]}>{item.change}</Badge>
                <span className="font-mono text-xs text-secondary break-all">{item.path}</span>
              </div>
              {item.review_required && (
                <p role="note" className="text-xs text-amber-400">
                  {category === 'sql' ? 'Manual SQL review required' : 'Manual review required'}
                </p>
              )}
              <dl className="grid gap-2 sm:grid-cols-2">
                <div className="space-y-1">
                  <dt className="text-xs text-muted">Before</dt>
                  <dd>
                    <pre className="max-h-48 overflow-auto rounded-md bg-surface px-2 py-1.5 text-xs text-muted whitespace-pre-wrap break-words">{JSON.stringify(item.before, null, 2)}</pre>
                  </dd>
                </div>
                <div className="space-y-1">
                  <dt className="text-xs text-muted">After</dt>
                  <dd>
                    <pre className="max-h-48 overflow-auto rounded-md bg-surface px-2 py-1.5 text-xs text-muted whitespace-pre-wrap break-words">{JSON.stringify(item.after, null, 2)}</pre>
                  </dd>
                </div>
              </dl>
            </article>
          ))}
        </section>
      ))}
    </section>
  )
}
