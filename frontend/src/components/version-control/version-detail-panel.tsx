import { Copy, RotateCcw, X } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { VersionDetail } from '@/types/version-control'
import { absoluteTime, friendlyActor, originMeta, relativeTime, shortId } from './version-format'

const CHIP = 'inline-flex items-center gap-1 rounded-md border border-default bg-surface px-2 py-0.5 text-xs text-muted'

function Fingerprint({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-xs text-muted">{label}</span>
      <span className="font-mono text-xs text-secondary truncate" title={value}>{shortId(value)}</span>
    </div>
  )
}

interface VersionDetailPanelProps {
  detail: VersionDetail
  onClose: () => void
  onRestore?: () => void
  restoreEnabled?: boolean
  restoring?: boolean
}

export function VersionDetailPanel({ detail, onClose, onRestore, restoreEnabled, restoring }: VersionDetailPanelProps) {
  const meta = originMeta(detail.origin)
  const { Icon } = meta
  const snapshot = detail.snapshot
  return (
    <section aria-label="Version detail" className="rounded-xl border border-default bg-surface p-4 space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <Badge variant={meta.variant} className="gap-1">
          <Icon className="w-3 h-3" />
          {meta.label}
        </Badge>
        <span className="font-mono text-xs text-secondary" title={detail.version_id}>{shortId(detail.version_id)}</span>
        <button
          type="button"
          onClick={() => { void navigator.clipboard?.writeText(detail.version_id) }}
          title="Copy full version id"
          aria-label="Copy full version id"
          className="text-muted hover:text-secondary transition-colors"
        >
          <Copy className="w-3 h-3" />
        </button>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close version detail"
          className="ml-auto text-muted hover:text-secondary transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted">Captured</span>
          <span className="text-secondary" title={absoluteTime(detail.observed_at)}>{relativeTime(detail.observed_at)}</span>
        </div>
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted">By</span>
          <span className="text-secondary truncate" title={detail.observed_by}>{friendlyActor(detail.observed_by)}</span>
        </div>
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted">Canonicalizer</span>
          <span className="font-mono text-secondary">{detail.fingerprints.canonicalizer_version}</span>
        </div>
      </div>

      <div className="space-y-1.5">
        <p className="text-xs font-semibold text-secondary uppercase tracking-wide">Fingerprints</p>
        <Fingerprint label="Config" value={detail.fingerprints.config} />
        <Fingerprint label="Benchmark" value={detail.fingerprints.benchmark} />
        <Fingerprint label="Metadata" value={detail.fingerprints.metadata} />
      </div>

      {(detail.parent_version_id || detail.restored_from_version_id || detail.optimizer_run_id) && (
        <div className="flex flex-wrap items-center gap-2">
          {detail.parent_version_id && (
            <span className={CHIP} title={`Parent version ${detail.parent_version_id}`}>Parent {shortId(detail.parent_version_id)}</span>
          )}
          {detail.restored_from_version_id && (
            <span className={CHIP} title={`Restored from ${detail.restored_from_version_id}`}>Restored from {shortId(detail.restored_from_version_id)}</span>
          )}
          {detail.optimizer_run_id && (
            <span className={CHIP}>Optimizer run · Champion {detail.champion_id ?? 'unknown'}</span>
          )}
        </div>
      )}

      {snapshot !== undefined && snapshot !== null && (
        <details className="rounded-lg border border-default bg-surface-secondary/40">
          <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-secondary">
            Restorable snapshot
          </summary>
          <pre className="max-h-80 overflow-auto px-3 pb-3 text-xs text-muted whitespace-pre-wrap break-words">
            {JSON.stringify(snapshot, null, 2)}
          </pre>
        </details>
      )}

      {onRestore && (
        <div className="flex items-center justify-end pt-1">
          <button
            type="button"
            onClick={onRestore}
            disabled={!restoreEnabled || restoring}
            title={restoreEnabled
              ? 'Apply this version as the live configuration (a new reviewed version; history is preserved)'
              : 'Restore is not enabled on this deployment'}
            className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-default text-sm font-medium text-secondary hover:bg-surface-secondary transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <RotateCcw className="w-4 h-4" />
            {restoring ? 'Restoring…' : 'Restore this version'}
          </button>
        </div>
      )}
    </section>
  )
}
