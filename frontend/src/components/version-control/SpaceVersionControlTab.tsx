import { useCallback, useEffect, useState } from 'react'
import { Camera, GitBranch, RefreshCw, Upload } from 'lucide-react'
import { cn } from '@/lib/utils'
import { VersionControlApi, VersionControlError } from '@/lib/version-control-api'
import type { ObservationResult, SemanticDiff, VersionDetail, VersionPage, VersionSummary } from '@/types/version-control'
import { History } from './history'
import { VersionDetailPanel } from './version-detail-panel'
import { SemanticDiffView } from './diff'
import { shortId } from './version-format'

type SubTab = 'versions' | 'promote'

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof VersionControlError ? err.message : fallback
}

// Real (non-demo) Version Control client. The observe surface is fully fail-closed on
// the backend: unless the deployment enables the VC flags, these calls return 503 and
// the tab renders an informative empty state.
const api = new VersionControlApi((input, init) => fetch(input, init))

const EMPTY_PAGE: VersionPage = { items: [], next_cursor: null }

interface Props {
  spaceId: string
}

export function SpaceVersionControlTab({ spaceId }: Props) {
  const [subTab, setSubTab] = useState<SubTab>('versions')
  const [page, setPage] = useState<VersionPage>(EMPTY_PAGE)
  const [loading, setLoading] = useState(false)
  const [capturing, setCapturing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [detail, setDetail] = useState<VersionDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [diff, setDiff] = useState<SemanticDiff | null>(null)
  const [diffError, setDiffError] = useState<string | null>(null)
  const [restoreEnabled, setRestoreEnabled] = useState(false)
  const [pendingRestore, setPendingRestore] = useState<VersionSummary | null>(null)
  const [restoring, setRestoring] = useState(false)
  const [restoreError, setRestoreError] = useState<string | null>(null)

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
    setDetail(null)
    setDetailError(null)
    setDiff(null)
    setDiffError(null)
    setPendingRestore(null)
    setRestoreError(null)
    void load()
  }, [load])

  // Deployment flags (whether restore is enabled) — best-effort; default disabled.
  useEffect(() => {
    let live = true
    api.config().then(cfg => { if (live) setRestoreEnabled(cfg.restore_enabled) }).catch(() => {})
    return () => { live = false }
  }, [])

  const selectVersion = useCallback(async (version: VersionSummary) => {
    setDetail(null)
    setDetailError(null)
    try {
      setDetail(await api.version(version.binding_id, version.version_id))
    } catch (err) {
      setDetailError(errorMessage(err, 'Failed to load version detail.'))
    }
  }, [])

  const compareVersions = useCallback(async (left: string, right: string) => {
    const bindingId = page.items[0]?.binding_id
    if (!bindingId) return
    setDiff(null)
    setDiffError(null)
    try {
      setDiff(await api.diff(bindingId, left, right))
    } catch (err) {
      setDiffError(errorMessage(err, 'Failed to compare versions.'))
    }
  }, [page.items])

  // Publish = in-workspace restore (CUJ-1 §4.5): preview current→selected, confirm, apply.
  const beginRestore = useCallback((version: VersionSummary) => {
    const current = page.items[0]?.version_id
    setRestoreError(null)
    setPendingRestore(version)
    if (current && current !== version.version_id) void compareVersions(current, version.version_id)
    else setDiff(null)
  }, [page.items, compareVersions])

  const confirmRestore = useCallback(async () => {
    const current = page.items[0]?.version_id
    if (!pendingRestore || !current) return
    setRestoring(true)
    setRestoreError(null)
    try {
      const result = await api.spaceRestore(
        spaceId,
        { version_id: pendingRestore.version_id, expected_current_version_id: current },
        crypto.randomUUID(),
      )
      setNotice(result.captured_version
        ? 'Restored this version as the live configuration.'
        : 'The live space already matches this version.')
      setPendingRestore(null)
      setDetail(null)
      setDiff(null)
      await load()
    } catch (err) {
      setRestoreError(err instanceof VersionControlError && err.status === 409
        ? 'The space changed since you opened this view. Refresh the history and try again.'
        : errorMessage(err, 'Restore failed.'))
    } finally {
      setRestoring(false)
    }
  }, [spaceId, pendingRestore, page.items, load])

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

  const subTabs: { id: SubTab; label: string }[] = [
    { id: 'versions', label: 'Versions' },
    { id: 'promote', label: 'Promote' },
  ]

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 p-2 rounded-lg border border-default bg-surface-secondary text-muted">
          <GitBranch className="w-4 h-4" />
        </span>
        <div>
          <h3 className="text-lg font-display font-semibold text-primary">Version Control</h3>
          <p className="text-sm text-muted mt-1 max-w-2xl">
            Manage this agent's configuration versions in-workspace, and promote an approved
            version to another workspace you manage.
          </p>
        </div>
      </div>

      <div className="inline-flex rounded-lg border border-default bg-surface-secondary p-0.5">
        {subTabs.map(tab => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setSubTab(tab.id)}
            className={cn(
              'px-3 py-1.5 rounded-md text-sm font-medium transition-colors',
              subTab === tab.id
                ? 'bg-surface text-primary shadow-sm'
                : 'text-muted hover:text-secondary',
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {subTab === 'versions' && (
        <div className="space-y-4">
          <div className="flex items-center justify-end gap-2">
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
              onSelect={version => { void selectVersion(version) }}
              onCompare={(left, right) => { void compareVersions(left, right) }}
            />
          </div>

          {detailError && (
            <div role="alert" className="text-sm rounded-lg border border-red-500/30 bg-red-500/10 text-red-400 px-3 py-2">
              {detailError}
            </div>
          )}
          {detail && (
            <VersionDetailPanel
              detail={detail}
              onClose={() => setDetail(null)}
              onRestore={() => beginRestore(detail)}
              restoreEnabled={restoreEnabled}
              restoring={restoring && pendingRestore?.version_id === detail.version_id}
            />
          )}

          {pendingRestore && (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 space-y-3">
              <p className="text-sm text-secondary">
                Restore version <span className="font-mono text-primary">{shortId(pendingRestore.version_id)}</span> as
                the live configuration? It is applied as a new version — history is preserved. Review the changes below.
              </p>
              {restoreError && (
                <div role="alert" className="text-sm rounded-lg border border-red-500/30 bg-red-500/10 text-red-400 px-3 py-2">
                  {restoreError}
                </div>
              )}
              <div className="flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => { setPendingRestore(null); setRestoreError(null) }}
                  disabled={restoring}
                  className="px-3 py-2 rounded-lg border border-default text-sm font-medium text-muted hover:text-secondary hover:bg-surface-secondary transition-colors disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => { void confirmRestore() }}
                  disabled={restoring}
                  className="px-3 py-2 rounded-lg bg-amber-500/20 border border-amber-500/40 text-sm font-medium text-amber-300 hover:bg-amber-500/30 transition-colors disabled:opacity-50"
                >
                  {restoring ? 'Restoring…' : 'Confirm restore'}
                </button>
              </div>
            </div>
          )}

          {diffError && (
            <div role="alert" className="text-sm rounded-lg border border-red-500/30 bg-red-500/10 text-red-400 px-3 py-2">
              {diffError}
            </div>
          )}
          {diff && <SemanticDiffView diff={diff} />}
        </div>
      )}

      {subTab === 'promote' && (
        <div className="rounded-xl border border-default bg-surface p-4">
          <div className="text-center py-16 text-muted">
            <Upload className="w-8 h-8 mx-auto mb-3 opacity-50" />
            <p className="text-secondary font-medium">Cross-workspace promotion</p>
            <p className="text-sm mt-1 max-w-md mx-auto">
              Promote an approved version to another workspace you manage, with two-person
              approval and a durable deployment receipt. This surface is not enabled on this
              deployment yet.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
