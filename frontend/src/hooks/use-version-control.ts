import { useEffect, useMemo, useSyncExternalStore } from 'react'
import type { VersionControlApi } from '@/lib/version-control-api'
import { VersionControlError } from '@/lib/version-control-api'
import type { BindingStatus, VersionPage } from '@/types/version-control'
import { demoApi } from '@/components/version-control/demo-api'

export interface VersionControlState {
  bindingId: string; loading: boolean; captured: boolean; busy: boolean; stale: boolean
  status: BindingStatus | null; history: VersionPage; error: string
}
const initialState = (): VersionControlState => ({ bindingId: '', loading: false, captured: false, busy: false, stale: false, status: null, history: { items: [], next_cursor: null }, error: '' })
export function canMutate(state: VersionControlState, action: string) {
  return Boolean(state.captured && !state.loading && !state.busy && !state.stale && state.status && !state.status.stale && !state.status.quarantined && !state.status.unresolved_operation_id && !['unknown', 'unreachable', 'applied_unverified', 'conflicted'].includes(state.status.drift) && state.status.allowed_actions.includes(action))
}
export function createVersionControlStore(api: VersionControlApi) {
  let state = initialState()
  const listeners = new Set<() => void>()
  const update = (changes: Partial<VersionControlState>) => { state = { ...state, ...changes }; listeners.forEach(listener => listener()) }
  return {
    getSnapshot: () => state,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } },
    async nextPage() {
      if (!state.history.next_cursor || state.loading) return
      update({ loading: true })
      try {
        const page = await api.versions(state.bindingId, state.history.next_cursor)
        update({ history: { items: [...state.history.items, ...page.items], next_cursor: page.next_cursor } })
      } catch (error) { update({ error: String(error), stale: true }) }
      finally { update({ loading: false }) }
    },
    async open(bindingId: string, reason: 'open' | 'history' | 'refresh' | 'return' = 'open') {
      update({ bindingId, loading: true, captured: false, error: '' })
      try {
        const observation = await api.observe(bindingId, reason, crypto.randomUUID())
        update({ status: observation.status, captured: !observation.busy && !observation.status.stale, busy: observation.busy, stale: observation.status.stale })
      } catch (error) {
        const label = error instanceof VersionControlError ? error.status === 423 ? 'Quarantined / unresolved: ' : error.status === 409 ? 'Conflicted; preserved history: ' : error.status === 503 ? 'Evidence unreachable: ' : '' : ''
        update({ captured: false, stale: true, error: label + (error instanceof Error ? error.message : 'Capture unavailable') })
      }
      try { update({ history: await api.versions(bindingId) }) }
      catch (error) { update({ stale: true, captured: false, error: `${state.error} History unavailable: ${String(error)}` }) }
      finally { update({ loading: false }) }
    },
  }
}
export function useVersionControl(bindingId: string, api: VersionControlApi = demoApi) {
  const store = useMemo(() => createVersionControlStore(api), [api])
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot)
  useEffect(() => { void store.open(bindingId) }, [store, bindingId])
  useEffect(() => {
    const onReturn = () => { void store.open(bindingId, 'return') }
    window.addEventListener('focus', onReturn)
    return () => window.removeEventListener('focus', onReturn)
  }, [store, bindingId])
  return { ...state, nextPage: store.nextPage, refresh: (reason: 'history' | 'refresh' = 'refresh') => store.open(bindingId, reason) }
}
