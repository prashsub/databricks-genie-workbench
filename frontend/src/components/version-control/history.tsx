import { useState } from 'react'
import { Copy, GitBranch, Sparkles } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import type { VersionPage, VersionSummary } from '@/types/version-control'
import { absoluteTime, friendlyActor, originMeta, relativeTime, shortId } from './version-format'

interface HistoryProps {
  page: VersionPage
  loading?: boolean
  onNext: () => void
  onSelect: (version: VersionSummary) => void
  onCompare: (left: string, right: string) => void
}

const CHIP = 'inline-flex items-center gap-1 rounded-md border border-default bg-surface px-2 py-0.5 text-xs text-muted'

function VersionRow({ version, onSelect }: { version: VersionSummary; onSelect: (version: VersionSummary) => void }) {
  const meta = originMeta(version.origin)
  const { Icon } = meta
  const hasLineage = version.parent_version_id || version.restored_from_version_id || version.optimizer_run_id
  return (
    <li className="rounded-lg border border-default bg-surface-secondary/40 p-3">
      <div className="flex items-center gap-2 flex-wrap">
        <Badge variant={meta.variant} className="gap-1">
          <Icon className="w-3 h-3" />
          {meta.label}
        </Badge>
        <button
          type="button"
          onClick={() => onSelect(version)}
          title={version.version_id}
          className="font-mono text-xs text-secondary hover:text-primary transition-colors"
        >
          {shortId(version.version_id)}
        </button>
        <button
          type="button"
          onClick={() => { void navigator.clipboard?.writeText(version.version_id) }}
          title="Copy full version id"
          aria-label="Copy full version id"
          className="text-muted hover:text-secondary transition-colors"
        >
          <Copy className="w-3 h-3" />
        </button>
        <span className="ml-auto text-xs text-muted" title={absoluteTime(version.observed_at)}>
          {relativeTime(version.observed_at)}
        </span>
      </div>

      <p className="mt-1.5 text-xs text-muted">
        by <span className="text-secondary" title={version.observed_by}>{friendlyActor(version.observed_by)}</span>
      </p>

      {hasLineage && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {version.parent_version_id && (
            <span className={CHIP} title={`Parent version ${version.parent_version_id}`}>
              Parent {shortId(version.parent_version_id)}
            </span>
          )}
          {version.restored_from_version_id && (
            <span className={CHIP} title={`Restored from ${version.restored_from_version_id}`}>
              Restored from {shortId(version.restored_from_version_id)}
            </span>
          )}
          {version.optimizer_run_id && (
            <a
              href={`?view=detail&space=${encodeURIComponent(version.binding_id)}&tab=optimize&run=${encodeURIComponent(version.optimizer_run_id)}`}
              className={`${CHIP} hover:text-accent hover:border-accent/40 transition-colors`}
              title={`Optimizer run ${version.optimizer_run_id}`}
            >
              <Sparkles className="w-3 h-3" />
              Optimizer run · Champion {version.champion_id ?? 'unknown'}
            </a>
          )}
        </div>
      )}
    </li>
  )
}

function LoadingRows() {
  return (
    <div className="space-y-2" aria-hidden="true">
      {[0, 1, 2].map(i => (
        <div key={i} className="flex items-start gap-3 rounded-lg border border-default p-3">
          <Skeleton className="h-5 w-20 rounded-full" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-3 w-40" />
            <Skeleton className="h-3 w-24" />
          </div>
        </div>
      ))}
    </div>
  )
}

function EmptyState() {
  return (
    <div className="text-center py-16 text-muted">
      <GitBranch className="w-8 h-8 mx-auto mb-3 opacity-50" />
      <p className="text-secondary font-medium">No versions captured yet</p>
      <p className="text-sm mt-1">Capture the current configuration to start tracking changes over time.</p>
    </div>
  )
}

export function History({ page, loading, onNext, onSelect, onCompare }: HistoryProps) {
  const [left, setLeft] = useState('')
  const [right, setRight] = useState('')

  if (loading && !page.items.length) return <LoadingRows />
  if (!page.items.length) return <EmptyState />

  const selectClass = 'rounded-md border border-default bg-surface px-2 py-1 text-xs text-secondary'
  const actionClass = 'inline-flex items-center gap-1 rounded-md border border-default px-2.5 py-1 text-xs font-medium text-secondary hover:bg-surface-secondary transition-colors disabled:opacity-50 disabled:cursor-not-allowed'

  return (
    <section aria-label="Captured history" className="space-y-3">
      <ol className="space-y-2">
        {page.items.map(version => (
          <VersionRow key={version.version_id} version={version} onSelect={onSelect} />
        ))}
      </ol>

      <div className="flex flex-wrap items-center gap-2 border-t border-default pt-3">
        <span className="text-xs uppercase tracking-wide text-muted">Compare</span>
        <label className="sr-only" htmlFor="vc-compare-left">Compare from</label>
        <select id="vc-compare-left" className={selectClass} value={left} onChange={event => setLeft(event.target.value)}>
          <option value="">Select version</option>
          {page.items.map(version => <option key={version.version_id} value={version.version_id}>{shortId(version.version_id)}</option>)}
        </select>
        <span className="text-xs text-muted">to</span>
        <label className="sr-only" htmlFor="vc-compare-right">Compare to</label>
        <select id="vc-compare-right" className={selectClass} value={right} onChange={event => setRight(event.target.value)}>
          <option value="">Select version</option>
          {page.items.map(version => <option key={version.version_id} value={version.version_id}>{shortId(version.version_id)}</option>)}
        </select>
        <button type="button" className={actionClass} disabled={!left || !right || left === right} onClick={() => onCompare(left, right)}>
          Compare versions
        </button>

        {page.next_cursor && (
          <button type="button" className={`${actionClass} ml-auto`} disabled={loading} onClick={onNext}>
            {loading ? 'Loading…' : 'Load more'}
          </button>
        )}
      </div>
    </section>
  )
}
