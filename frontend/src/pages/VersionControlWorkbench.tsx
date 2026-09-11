import { useEffect, useRef, useState } from 'react'
import { VersionControlOverview } from '@/components/version-control/overview'
import { VersionControlTab } from '@/components/version-control/version-control-tab'
import { demoApi } from '@/components/version-control/demo-api'

export function VersionControlWorkbench() {
  const [bindingId, setBindingId] = useState(() => typeof window === 'undefined' ? '' : new URLSearchParams(window.location.search).get('binding') ?? '')
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { if (bindingId) heading.current?.focus() }, [bindingId])
  return <main className="min-h-screen bg-background text-primary p-6 mx-auto max-w-6xl space-y-6 [&_button]:rounded [&_button]:border [&_button]:border-border [&_button]:px-3 [&_button]:py-2 [&_button]:m-1 [&_button:disabled]:opacity-50 [&_input]:border [&_input]:border-border [&_input]:rounded [&_input]:p-2 [&_input]:m-2 [&_select]:border [&_select]:p-2 [&_pre]:overflow-auto [&_pre]:text-xs [&_h3]:font-semibold [&_section]:space-y-3">
    <header className="space-y-2">
      <h1 className="text-2xl font-bold">VC/1.0 API-faked foundation</h1>
      <p role="note">No real endpoints. All history, identities, votes, jobs and receipts are local fixtures; nothing is deployed.</p>
      <p>Demo approvals include a seeded first reviewer; voting acts as the second fixture reviewer, never as a supplied identity.</p>
      <p>Promotion example: target <code>demo-target</code>, mapping <code>mapping-1</code>, policy <code>production</code>.</p>
      <a href="?view=list">Leave demo and return to Agents</a>
    </header>
    {bindingId ? <>
      <button onClick={() => setBindingId('')}>Back to version overview</button>
      <h2 ref={heading} tabIndex={-1}>Agent binding: {bindingId}</h2>
      <VersionControlTab key={bindingId} bindingId={bindingId} api={demoApi} />
    </> : <VersionControlOverview api={demoApi} onSelect={setBindingId} />}
  </main>
}
