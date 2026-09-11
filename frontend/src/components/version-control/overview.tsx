import type { VersionControlApi } from '@/lib/version-control-api'
import { useEffect, useState } from 'react'
import type { OverviewPage } from '@/types/version-control'
import { OverviewBadge } from './overview-badge'
import { loadOverview } from './overview-actions'

export function VersionControlOverview({ api, onSelect }: { api: VersionControlApi; onSelect: (bindingId: string) => void }) {
  const [page, setPage] = useState<OverviewPage>({ items: [], next_cursor: null })
  const [cursor, setCursor] = useState<string>()
  const [refresh, setRefresh] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    // Show the spinner immediately when the query inputs change; a deps-triggered
    // fetch legitimately resets loading synchronously (error clears on settle).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true)
    loadOverview(api, cursor, controller.signal).then(result => {
      if (!controller.signal.aborted) { setPage(result); setError('') }
    }).catch(error => { if (!controller.signal.aborted) setError(String(error)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [api, cursor, refresh])
  return <section aria-label="Version overview" className="space-y-4">
    <h2>Version Control overview</h2>
    <button onClick={() => setRefresh(value => value + 1)} disabled={loading}>Refresh projections</button>
    {loading && <p role="status">Loading projections…</p>}
    {error && <p role="alert">Stale overview: {error}</p>}
    {!loading && !error && page.items.length === 0 && <p>No enrolled bindings.</p>}
    <ul>{page.items.map(status => <li key={status.binding_id}>
      <button onClick={() => onSelect(status.binding_id)}>{status.binding_id}</button>
      <OverviewBadge api={api} status={status} />
    </li>)}</ul>
    <button disabled={loading || !cursor} onClick={() => setCursor(undefined)}>First page</button>
    <button disabled={loading || !page.next_cursor} onClick={() => setCursor(page.next_cursor ?? undefined)}>Next page</button>
  </section>
}
