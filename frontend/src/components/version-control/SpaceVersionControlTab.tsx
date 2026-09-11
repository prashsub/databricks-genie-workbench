import { useCallback, useEffect, useState } from 'react'
import { Camera, GitBranch, RefreshCw } from 'lucide-react'
import { VersionControlApi, VersionControlError } from '@/lib/version-control-api'
import type { ObservationResult, VersionPage } from '@/types/version-control'
import { History } from './history'

// Real (non-demo) Version Control client. The observe surface is fully fail-closed on
// the backend: unless the deployment enables the VC flags, these calls return 503 and
// the tab renders an informative empty state.
const api = new VersionControlApi((input, init) => fetch(input, init))

const EMPTY_PAGE: VersionPage = { items: [], next_cursor: null }

interface Props {
  spaceId: string
}

export function SpaceVersionControlTab({ spaceId }: Props) {
  const [page, setPage] = useState<VersionPage>(EMPTY_PAGE)
  const [loading, setLoading] = useState(false)
  const [capturing, setCapturing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async (cursor?: string) => {
    setLoading(true)
    setError(null)
    try {
      const next = await api.spaceVersions(spaceId, cursor)
      setPage(prev => cursor
        ? { items: [...prev.items, ...next.items], next_cursor: next.next_cursor }
        : next)
    } catch (err) {
      setPage(EMPTY_PAGE)
      setError(err instanceof VersionControlError ? err.message : 'Failed to load version history.')
    } finally {
      setLoading(false)
    }
  }, [spaceId])

  useEffect(() => {
    setNotice(null)
    void load()
  }, [load])

  const capture = useCallback(async () => {
    setCapturing(true)
    setError(null)
    setNotice(null)
    try {
      const result: ObservationResult = await api.spaceObserve(spaceId, crypto.randomUUID())
      setNotice(result.captured_version
        ? 'Captured the current configuration as a new version.'
        : 'No change since the last captured version.')
      await load()
    } catch (err) {
      setError(err instanceof VersionControlError ? err.message : 'Capture failed.')
    } finally {
      setCapturing(false)
    }
  }, [spaceId, load])

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 p-2 rounded-lg border border-default bg-surface-secondary text-muted">
            <GitBranch className="w-4 h-4" />
          </span>
          <div>
            <h3 className="text-lg font-display font-semibold text-primary">Version Control</h3>
            <p className="text-sm text-muted mt-1 max-w-2xl">
              Captured snapshots of this agent's configuration — including the before/after
              state around each optimizer run. Version history is read-only; capturing records
              the current configuration so you can track and (later) restore changes.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => load()}
            disabled={loading || capturing}
            className="p-2 rounded-lg border border-default text-muted hover:text-secondary hover:bg-surface-secondary transition-colors disabled:opacity-50"
            title="Refresh history"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={capture}
            disabled={capturing}
            className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-default text-sm font-medium text-secondary hover:bg-surface-secondary transition-colors disabled:opacity-50"
          >
            <Camera className="w-4 h-4" />
            {capturing ? 'Capturing…' : 'Capture current state'}
          </button>
        </div>
      </div>

      {error && (
        <div role="alert" className="text-sm rounded-lg border border-red-500/30 bg-red-500/10 text-red-400 px-3 py-2">
          {error}
        </div>
      )}
      {notice && (
        <div className="text-sm rounded-lg border border-default bg-surface-secondary text-muted px-3 py-2">
          {notice}
        </div>
      )}

      <div className="rounded-xl border border-default bg-surface p-4">
        <History
          page={page}
          loading={loading}
          onNext={() => { if (page.next_cursor) void load(page.next_cursor) }}
          onSelect={() => { /* version detail deferred to a later milestone */ }}
          onCompare={() => { /* semantic diff deferred to a later milestone */ }}
        />
      </div>
    </div>
  )
}
