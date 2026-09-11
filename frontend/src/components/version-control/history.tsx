import { useState } from 'react'
import type { VersionPage, VersionSummary } from '@/types/version-control'

interface HistoryProps {
  page: VersionPage; loading?: boolean; onNext: () => void
  onSelect: (version: VersionSummary) => void; onCompare: (left: string, right: string) => void
}
export function History({ page, loading, onNext, onSelect, onCompare }: HistoryProps) {
  const [left, setLeft] = useState('')
  const [right, setRight] = useState('')
  return <section aria-label="Captured history">
    <h3>Captured history</h3>
    {!page.items.length && <p>No captured versions.</p>}
    <ol>{page.items.map(version => <li key={version.version_id} className="border-b border-border py-3">
      <button onClick={() => onSelect(version)} title={version.version_id}>{version.version_id}</button>
      <p>{version.origin} · Observed by {version.observed_by} · <time>{version.observed_at}</time></p>
      <p>Parent: {version.parent_version_id ?? 'None'} · Restored from: {version.restored_from_version_id ?? 'None'}</p>
      {version.optimizer_run_id && <p><a href={`?view=detail&space=${encodeURIComponent(version.binding_id)}&tab=optimize&run=${encodeURIComponent(version.optimizer_run_id)}`}>Optimizer run {version.optimizer_run_id}</a> · Champion {version.champion_id ?? 'Unknown'}</p>}
    </li>)}</ol>
    <label>Compare from<select value={left} onChange={event => setLeft(event.target.value)}><option value="">Select version</option>{page.items.map(version => <option key={version.version_id}>{version.version_id}</option>)}</select></label>
    <label>Compare to<select value={right} onChange={event => setRight(event.target.value)}><option value="">Select version</option>{page.items.map(version => <option key={version.version_id}>{version.version_id}</option>)}</select></label>
    <button disabled={!left || !right || left === right} onClick={() => onCompare(left, right)}>Compare versions</button>
    <button disabled={loading || !page.next_cursor} onClick={onNext}>Next history page</button>
  </section>
}
