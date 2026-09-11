# CUJ 1 — Per-Agent Version Management

**Status:** Draft (locked scope)
**Last updated:** September 11, 2026
**Scope:** Per-agent, single-workspace only. Cross-workspace promotion is a separate journey — see [CUJ 2](cuj-2-cross-workspace-promotion.md).
**Architecture reference:** [README](README.md) · [ADR-001](ADR-001-api-observed-versioning.md)

## 1. Purpose

Give a user working on one Genie Agent a coherent, in-workspace surface to:

1. **See** the agent's captured configuration history (manual captures + the before/after
   snapshots around optimizer runs).
2. **Understand** what changed (inspect a version; diff two versions).
3. **Act** in-workspace — **capture** the current live state on demand, and **publish**
   (restore / roll-forward) a chosen version as a new reviewed write.

"Publish" in this CUJ means **restore/roll-forward within the same workspace** — making a
chosen historical (or newer captured) version the live configuration of *this* agent. It
never crosses a workspace boundary. All dev→prod movement lives in CUJ 2.

## 2. Surface

`SpaceDetail → Version Control` tab, presented as a segmented control with two sub-tabs:

```
Version Control
├─ Versions   ← this CUJ (default)
└─ Promote    ← CUJ 2 (cross-workspace)
```

Rationale for sub-tabs (not a modal/drawer): version history and promotion both have
durable, resumable state a user returns to; a persistent surface fits better than a
transient dialog. Keeping both under one per-agent tab preserves shared context (the same
binding; the version a user selects here becomes the `source_version_id` in CUJ 2).

## 3. Current state (what is built today)

| Capability | Backend | Status in running app |
|---|---|---|
| List versions | `GET /api/version-control/spaces/{id}/versions` → `ledger.history` | **Mounted, live** (gated on `VC_HISTORY_ENABLED`) |
| Capture current state | `POST /api/version-control/spaces/{id}/observe` → `Observer.capture_on_open` | **Mounted, live** (gated on `VC_WRITES_ENABLED`) |
| Version detail | `ledger.get_version` | Engine exists; **detail route not mounted** |
| Semantic diff | `canonical_diff.py` + `SemanticDiffView` | Engine exists; **diff endpoint deferred** (per observe-only pivot) |
| Publish (= in-workspace restore) | `POST .../restore` with `RestoreCommand` | Router present, **wired to `FailClosedRestore()`** (`VC_RESTORE_ENABLED=false`) |

The tab shell (`SpaceVersionControlTab.tsx`) is already on the design system; the body
(`history.tsx`) is a raw-HTML prototype and is the visual debt to replace.

## 4. The journey

### 4.1 View history (read; always available when VC is enabled)

A timeline/list of versions, newest first, paginated. Each row:

- **Short, copyable version id** (monospace), full id on hover.
- **Origin badge**, color-coded: `manual`, `optimizer`, `restore`, `promotion`, `external`.
- **Relative time** ("2h ago") with absolute timestamp on hover.
- **Actor**, resolved to a friendly label (the app SP GUID renders as
  "Workbench service principal", not `20d58fdb-…`).
- **Provenance chips** only when present: optimizer-run link + champion (`optimizer_run_id`,
  `champion_id`); lineage (`parent_version_id`, `restored_from_version_id`) shown only when
  non-null.
- **Optimizer before/after pairs** are visually grouped (a bracket/connector), because the
  headline value is "what the optimizer changed."

### 4.2 Capture current state (governed write; passive + on-demand)

- A **Capture** action reads live state and appends a new version **only if fingerprints
  changed** ("No change since the last captured version" otherwise).
- **Origin semantics fix:** a user-initiated capture must surface as `origin=manual`, not
  `external`. `external` is reserved for drift observed by capture-on-open. (This is a
  labeling change from today's behavior, where manual capture records `external`.)
- Disabled with an explainer when `VC_WRITES_ENABLED=false`.

### 4.3 Inspect a version (read)

Row → detail: fingerprints (config / benchmark / metadata), canonicalizer version,
provenance, lineage, and the restorable snapshot. Requires mounting a `get_version` detail
route.

### 4.4 Diff two versions (read)

Pick A and B → categorized, field-level diff (data sources, columns, instructions, joins,
filters, example SQL, benchmarks, metadata) rather than raw JSON. Engine (`canonical_diff`)
and view (`SemanticDiffView`) exist; needs a mounted diff endpoint. **Open:** ship now vs
defer (see §7).

### 4.5 Publish = in-workspace restore / roll-forward (governed write)

Publishing a selected version applies it to *this* agent as a **new reviewed write**,
never rewinding history:

1. Show the historical→current semantic diff.
2. Confirm the current base fingerprint still matches what the user reviewed.
3. Submit `RestoreCommand{version_id, binding_revision, expected_base, approval_id}` →
   `POST .../restore`.
4. Read back; persist the resulting version linked by `restored_from_version_id`.

Today the restore port is `FailClosedRestore()` and `VC_RESTORE_ENABLED=false`, so the
button renders **disabled with an explainer** ("Restore is not enabled on this
deployment"). When enabled, it runs through the governed gate (coordination lease,
pre-state capture, read-back verification) described in the [README](README.md).

## 5. States (must all be designed, not just the happy path)

- **VC disabled on this deployment** — distinct empty state (not "no versions").
- **Agent not enrolled yet** — distinct empty state.
- **No captures yet** — distinct empty state with a Capture call-to-action.
- **Loading** — skeleton rows, not blank.
- **Capturing** — inline status; success ("captured") vs no-op ("no change") notices.
- **Error** — inline alert (fail-closed; history shown stale, actions disabled).
- **Restore disabled** — explainer, not a dead button.

## 6. New vs reuse (scope for implementation)

- **Reuse (live):** versions list + capture endpoints; `ledger`, `Observer`,
  `canonical_diff`.
- **New (small):** version-detail route; (optional) diff endpoint; the `origin=manual`
  labeling fix; the redesigned `history.tsx` + `Versions` sub-tab on the design system.
- **Gated/deferred:** publish/restore stays fail-closed until `VC_RESTORE_ENABLED` and the
  governed write path are turned on.

## 7. Open questions

1. Ship **diff now** (engine exists) or defer per the observe-only pivot?
2. Is the `origin=manual` relabel a backend change (preferred, at capture time) or a
   frontend display mapping?
3. Does "publish/restore" ship **disabled-with-explainer** in the first PR, or is enabling
   the governed restore path in scope?

## 8. Non-goals

- No cross-workspace movement (CUJ 2).
- No account-wide / all-agents overview surface (both CUJs are per-agent).
- No automatic semantic merge.
