# Headless Workbench — CLI + MCP for Genie Agent Lifecycle

**Status:** Research / brainstorming (no code written). Docs-only feature branch `feature/headless-cli-mcp`.

**Goal.** Make Genie Workbench usable *without* the Databricks App UI, so that:

1. Coding agents and humans can drive the full Genie Agent lifecycle (inspect,
   scan, analyze, optimize, version, promote) from a **CLI**.
2. Hosted/server-side agents that cannot shell out can drive the same lifecycle
   through an **MCP server**.
3. Workbench capabilities can be published into **Databricks Agent Skills**,
   **Unity Catalog Skills**, **managed/registered MCP**, and **Genie Code /
   Agent Bricks Supervisor** as governed, discoverable tools.
4. Teams that prefer everything programmatic (no UI) can adopt Workbench with
   the smallest possible Databricks footprint.

This document captures the **converged architecture** from a two-model debate
(Claude/opus + GPT/astra), rooted in the actual code of this repository. It is a
research artifact, not an implementation plan.

---

## 1. Core thesis (both models converged)

**This is an *extraction* project, not a *packaging* project.** The MCP server
and CLI are thin adapters and are the easy part. The real, risky work is
untangling what already exists:

- **Ambient identity.** `backend/services/genie_client.py` and others resolve a
  `WorkspaceClient` from an ambient `ContextVar` via `get_workspace_client()`,
  called at 15+ sites **inside services as well as routers** (e.g.
  `backend/services/scanner.py:168`). There is no explicit context passed in.
- **Business logic in routers.** `backend/routers/auto_optimize.py` is ~3,849
  lines with 40+ inline `_build_* / _derive_* / _parse_* / _patch_for_ui`
  helpers — UI/derivation logic living in the HTTP layer, not a reusable service.
- **Process-bound creation.** Agent creation is an SSE `StreamingResponse` LLM
  loop with server-side, in-memory session state
  (`backend/services/create_agent_session.py`, plus ~3,446 lines of
  `create_agent_tools.py`). It initially *looked* un-headless-able, but a later
  code-rooted round found the transport seam is ~4 lines and the turn protocol is
  already externalized — so create is **full scope** on CLI and MCP. See §8a.
- **Already-clean domain.** `backend/services/config_fingerprint.py` is 261
  lines with **zero** databricks/auth/sdk imports — moving it to a domain layer
  is a `git mv`. A refactor plan whose first-named step is this free rename is
  mis-scoped.

**Design consequence:** one headless **`workbench-core`** library (no FastAPI,
no `ContextVar`, no `HTTPException`), with three thin adapters — **CLI**, **MCP**,
and the existing **FastAPI App** — all calling core directly. No adapter wraps
another (the MCP server must not shell out to the CLI, and the CLI must not call
the HTTP API).

---

## 2. Layered architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ Interfaces (thin, translate-only)                                      │
│   CLI (gwb …)      MCP server (workbench tools)      FastAPI App (UI)   │
└───────────────┬──────────────────┬───────────────────────┬────────────┘
                │ build WorkbenchContext at each edge       │
                ▼                  ▼                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Application (use-cases): CreateAgentFromConfig, ScanAgent,             │
│   AnalyzeAgent, OptimizeAgent(apply_mode), SnapshotVersion,           │
│   DiffVersions, RestoreVersion, PlanPromotion, ApplyPromotion         │
│   → own AUDIT + PRECONDITIONS + coarse execution policy (NOT authz)   │
└───────────────┬──────────────────────────────────────────────────────┘
                │ narrow ports
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Infrastructure (adapters behind ports):                               │
│   GenieTransport · WarehouseQuery · Ledger · ExecutionBackend ·        │
│   IdentityProvider · Clock                                            │
└───────────────┬──────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Domain (pure, no I/O): canonicalization, fingerprints, semantic diff, │
│   manifests, mapping. `config_fingerprint.py` moves here unchanged.   │
└──────────────────────────────────────────────────────────────────────┘
```

### Key ports

| Port | Purpose | Notes |
|---|---|---|
| `IdentityProvider` | Builds the explicit `WorkbenchContext` (client, capabilities, execution identity) at each edge | **Replaces the ambient `ContextVar`.** Reuses the VC design's identity port. |
| `GenieTransport` | Genie GET/PATCH | Wraps existing `genie_client.py`; `call_with_sp_fallback` policy made explicit (see §6). |
| `WarehouseQuery` | Delta / table reads | Wraps `sql_warehouse_query(ws, warehouse_id, sql)` — **always needs a live remote warehouse**. |
| `Ledger` | Version storage | Two impls: `DeltaLedger` (governed) and `FileLedger` (git-friendly JSON). See §4. |
| `ExecutionBackend` | Where long ops run | `JobsExecutionBackend` (real, wraps `GSO_JOB_ID`) + optional inline for dry-run/tests. |
| `Clock` | Time | Injectable for deterministic tests. |

---

## 3. Migration strategy — invert the ContextVar (do not delete it)

The migration hinge already exists in code: **`run_in_context`**. Rather than a
big-bang removal of ambient auth across 15+ sites (how such refactors stall):

1. Define an explicit `WorkbenchContext`, constructed at each entry point
   (CLI edge, MCP edge, FastAPI dependency).
2. Use `run_in_context` as the compatibility bridge; re-implement
   `get_workspace_client()` as a **deprecated reader** of `ctx.client` for
   unmigrated call sites.
3. Extract use-cases one at a time, replacing their ambient dependencies with
   injected ports.
4. **Enforce immediately but narrowly:** a lint/test forbids *new* ambient-auth
   call sites, and asserts **zero `services.auth` imports in extracted `core`
   modules** — not across the entire legacy tree overnight.
5. Replace the `is_running_on_databricks_apps()` environment sniff with an
   explicit `capabilities` field on the context, set at the edge. The FastAPI
   adapter may use the sniff only as a *default*. Capability fields describe
   supported behavior; **they are not authorization evidence.**

---

## 4. Version storage — injectable `Ledger` (enables no-UI teams)

The single most valuable idea for programmatic/no-UI teams: version storage is a
port with two implementations.

- **`DeltaLedger`** — the governed UC Delta ledger from the version-control
  design (immutable facts + coordination row). Full concurrency/approval
  guarantees.
- **`FileLedger`** — a git-friendly JSON directory. Gives a no-UI team immutable
  snapshots, history, semantic diffs, and **exportable promotion plans** with
  *zero Databricks-side provisioning* — and makes a promotion package reviewable
  in a pull request, which is exactly how programmatic teams want to approve a
  prod change.

**Boundary (converged):** artifact storage and release *coordination* are
different responsibilities.

- `FileLedger` may store immutable artifacts and **feed** a PR-based approval
  workflow.
- `FileLedger` **cannot** implement the CAS coordination row, so it reports
  `supports_concurrency_control: false` and **cannot itself enforce approval or
  coordinate a multi-writer deployment**. Those flows require the coordinated
  release executor (`DeltaLedger` + Jobs).
- "Zero provisioning" applies to **offline artifact work only** — warehouse-backed
  inspection and optimization still require a live warehouse (and Jobs).

---

## 5. Environment reporting — per-capability, not rigid tiers

There is no local execution: Delta reads go through
`sql_warehouse_query(ws, warehouse_id, sql)` (no databricks-connect). "Local"
can only ever mean *no Job, no Lakebase* — never *no Databricks*. **Do not
promise a production local optimizer**; `genie-space-optimizer` assumes a
warehouse + Delta and already runs as a Job.

Capability availability is a useful mental model (roughly: API-only →
warehouse-backed → Job/ledger-backed), but the **authoritative surface is a
`describe_environment` capability probe** that reports, per capability, whether
it is available and — if not — the exact missing requirement:

> `optimization: unavailable — missing GSO_JOB_ID`
> `ledger_versions: unavailable — no catalog/schema configured`

This is strictly more actionable than a coarse "tier unavailable" error, and it
prevents agents from reporting version control as *broken* when it is merely
*unprovisioned*. Tiers may be presented as a summary view over the probe.

---

## 6. Identity, fallback, and authorization

- **No Workbench-side authorization layer.** Today authorization is fully
  delegated to Databricks — the OBO token can PATCH a Genie space or it cannot.
  Building a Workbench-side authz model creates a second policy surface that
  drifts from UC grants and produces false confidence. **Use-cases own audit +
  preconditions + coarse *execution policy* only** (write toolsets off by
  default; prod promotion requires approval). Authorization stays delegated.

- **`call_with_sp_fallback` — the substitution rule (resolved from code).**
  The fallback in `genie_client.py` fires **only on OAuth *scope* errors**
  (`insufficient_scope`), **not** on 403/permission denials. It exists because
  the app's `dashboards.genie` user scope doesn't cover a few Conversation-API
  read endpoints (permissions, comments), and when it fires it logs a WARNING
  and **runs under the service-principal identity, not the user's**. Design rules:
  - Identity substitution is **scope-only and read-context**.
  - It must **never** be extended to writes (PATCH/restore/promote).
  - A permission **denial** must **never** be silently retried as the SP.
  - The core records the *actual* execution identity on every operation
    (`initiated_by`, `executed_as`).

---

## 7. CLI ↔ MCP parity contract

- **1:1 capability mapping.** Every CLI subcommand has an identically-named MCP
  tool with the same parameters, both normalizing to the **same application
  request**. Example: `gwb versions diff --from 3 --to 5 --json` ≡ MCP
  `diff_versions{from:3,to:5}`.
- **Test identical *structured results* under fixed fixtures**, plus each
  transport's error mapping — **not** byte-identical envelopes. Envelopes,
  operation IDs, and timestamps legitimately differ between transports and
  between runs. One cross-adapter fixture suite runs the same fixtures through
  both transports in CI.
- **Do not contort** CLI command paths to match MCP tool names literally; map at
  the request level.

---

## 8. MCP tool surface (control plane, not data plane)

The **managed Genie MCP** is the *data plane* (ask questions / query a space).
The **Workbench MCP** is the *control plane* (author / measure / evolve /
govern). **Do not duplicate** the managed Genie MCP — no `ask` / `query_space`
tools.

Toolsets, enabled independently and off-by-default for writes:

| Toolset | Example tools | Tier |
|---|---|---|
| `read` (default) | `list_agents`, `get_agent`, `get_config`, `validate_config`, `diff_versions`, `describe_environment` | API-only |
| `analyze` | `scan_agent`, `profile_tables`, `analyze_agent` | warehouse |
| `optimize` | `start_optimize_run` → `get_optimize_run` → `apply_optimize_run{expected_fingerprint}` | Job |
| `versions` | `snapshot_version`, `list_versions`, `restore_version` | ledger |
| `promotion` | `plan_promotion`, `apply_promotion` (bound to immutable version + approved plan + target fingerprint) | ledger + Jobs |
| `create` | `create_agent_start`, `create_agent_turn` (one call = one turn), `create_agent_get_plan`, `create_agent_apply_plan`, `create_agent_abandon` | warehouse (Tier 1) |

Design rules for MCP:
- **Long ops never block.** They return an `operation_id`; callers poll
  `get_*`. See §9.
- **Optimize is propose-only by default** for non-deterministic callers.
  `apply_mode` stays in the *core* use-case (App + CI want direct apply); the
  MCP surface splits it into `start → get → apply`, and a separately-permissioned
  automation surface may retain direct-apply.
- **Big configs/diffs spill to MCP resources** (`genie://…`) rather than inline
  payloads.
- **Write tools bind `expected_fingerprint` + `idempotency_key`.**
  `expected_fingerprint` is a **precondition, not proof of atomic CAS** — atomic
  compare-and-swap remains the `DeltaLedger` coordination row's job.
- **Conversational create is FULL SCOPE on both CLI and MCP** (requirement
  reversal — see §8a). It reuses the *existing* turn protocol: `create_agent.chat()`
  is already an async generator of transport-agnostic events, the turn boundary is
  already externalized (`needs_continuation` / `MAX_TOOL_ROUNDS`), so **MCP needs no
  streaming** — one `create_agent_turn` call = one turn. Writes are gated two-phase:
  the LLM plans with `create_space`/`update_space` **suppressed**, then a caller-signed
  plan object is applied via the deterministic `_fast_create` path. No LLM-authored
  write ever reaches Genie PATCH without explicit caller approval.

---

## 8a. Conversational create — the headless seam (full scope)

A later stress-test round **reversed the earlier decision to defer create**. Create
must be reachable headless on both CLI and MCP. Re-reading the code showed this is
far cheaper than feared — the seam already exists:

- `CreateGenieAgent.chat()` is **already an async generator of transport-agnostic
  dicts** (`tool_call`, `message_delta`, `created`, `done`). The SSE coupling is
  ~4 lines (`_sse_event()`); `event_stream()` is a keepalive wrapper.
- The **turn boundary is already externalized**: `done.needs_continuation`,
  `continuation_count`, `MAX_TOOL_ROUNDS = 15`; the frontend continues by POSTing an
  empty message. So the loop is already request/response-per-turn — **MCP needs no
  streaming and no polling infrastructure**; one tool call = one turn.
- A **deterministic no-LLM apply path already exists** (`_fast_create`:
  `generate_config → validate_config → create_space`), and `present_plan` is a pure
  pass-through.
- Session persistence is **already a half-built port** (L1 in-memory + L2 Lakebase).

**Extraction shape (not a rewrite, not a parallel batch path):**

```
core/usecases/create/
  session.py    CreateSession state (serializable; NO _lock, clients, generators)
  ports.py      SessionStore { InMemory | File | Lakebase/Delta }
  turn.py       run_turn(ctx, session, message, selections) -> AsyncIterator[Event]
  drive.py      run_to_completion(...)  # loops run_turn while needs_continuation
```

- **Web**: unchanged (keeps `_sse_event`).
- **CLI**: `gwb create chat --session S -m "…" --stream ndjson`, `--interactive`
  prompts on `pending_decision`; plus explicit `resume`/`message`/`approve` commands.
- **MCP**: the create toolset above; one turn per call; returns transcript +
  `next_action` (`waiting_for_input` / `waiting_for_approval` / `done`).
- **Batch create** (convenience) is a driver loop over the same turn protocol —
  **never a third code path**.

**Two pre-existing bugs to fix in P0 regardless** (durable/headless sessions make
them exploitable): (1) `get_session_async()` performs **no identity check** — bind
`created_by` at start and verify per turn, or a leaked `session_id` is session
hijack; (2) duplicate-create-on-timeout is already a live bug
(`test_create_timeout_dedup.py`) — an `idempotency_key` on `create_space`/`update_space`
is a hard prerequisite. `AgentSession._lock` is a process-local `asyncio.Lock` that
silently vanishes under durable multi-process sessions — replace with a `turn_seq`
optimistic-CAS in the store that rejects out-of-order turns.

---

## 9. Unified operation model

Long-running work already returns a **Jobs handle** today (`run_id` /
`job_run_id` / `job_url`) — but that leaks scheduler semantics to callers. Unify
on **one lifecycle concept shared with the version-control design**:

```
Operation {
  operation_id,           # the VC design's operation_id — ONE lifecycle concept
  kind,                   # optimize | promote | restore | reconcile
  status,                 # pending | running | succeeded | failed | quarantined
  external_ref: { job_run_id, job_url }   # Jobs handle lives here
}
```

Reuse Databricks Jobs for execution + status; **do not build a competing
scheduler.** Persist the `operation_id → job_run_id` mapping where hosted
callers need durable lookup.

---

## 10. Integration surfaces (see `platform-research.md` for sources)

| Surface | What it is (2026) | How Workbench plugs in |
|---|---|---|
| **Databricks Agent Skills** | `SKILL.md` instruction files (open Agent Skills spec), installed via `databricks aitools`; official `databricks/databricks-agent-skills` repo | Ship Workbench skills that teach an assistant to drive the CLI/MCP. Author knowledge **once**; SKILL.md references it. |
| **Unity Catalog Skills** | Skills as governed UC securables (`catalog.schema.skill`) — centrally permissioned + discoverable (Beta) | `gwb skills publish` publishes the same bundle as a governed UC object — **a distribution target, never a second implementation**. |
| **Managed MCP + MCP Service registry** | Managed MCP servers (Genie, AI Search, SQL, UC Functions) + external MCP registered in the Unity AI Gateway under UC governance | Run the Workbench MCP server; register it as an MCP Service (grant `EXECUTE`, restrict exposed tools). |
| **Genie Code / Agent Bricks Supervisor** | Agents that route to Genie spaces, UC functions, MCP, Knowledge Assistants | Supervisor routes to Workbench MCP tools (read/assessment subset by default). |

**Skills rule (converged):** SKILL.md = instructions; UC Skill = governed
*publication* of the same bundle; MCP/CLI = the validated executable behavior.
SKILL.md is written against a **capability contract** with a preamble: *"if `gwb`
is on PATH use it; otherwise use the identically-named Workbench MCP tools"* —
**but respect an explicitly selected remote/governed mode** rather than blindly
preferring any `gwb` found on PATH.

**Fallback safety rule:** falling back to another configured transport on
**transport absence** at skill start is fine; falling back after an
**authorization denial** is forbidden (that is a governance bypass).

CI dry-runs every command/tool referenced in every SKILL.md.

---

## 11. Packaging & deployment

- One versioned pip package with extras `[cli] / [mcp] / [app]`, exposing a
  `gwb` entrypoint.
- **CLI:** standalone, JSON output, stable exit codes.
- **MCP:** local via stdio by default; hosted as a headless App with OBO for the
  governed shared deployment, then registered in the AI Gateway.
- **Jobs** run optimization workers — **never** the interactive MCP endpoint.
- **DABs** are a deployment *template*, not the universal installer.
- The ledger is **per-workspace UC**, so a per-workspace deployment of the
  hosted MCP/App is unavoidable for governed operations.

---

## 11a. Regression strategy & safety (the core risk)

A ~7k-line refactor is regression-prone — so the strategy is **extract, don't
rewrite**, proven by the fact that this repo *already did this once* (PR-0
extracted scanner scoring into `genie_space_optimizer.iq_scan.scoring`, guarded by
`test_scanner_parity.py`, which asserts `backend.calculate_score is
gso.calculate_score` **and** byte-identical fixtures). That "re-export, don't
re-implement + identity assertion" pattern is the template for every extraction.

**1. Golden JSON is necessary but NOT sufficient — freeze effect traces.**
Response-body goldens miss the dangerous half. Also capture, under scripted
dependencies (fixed clock/id/model), an **effect log**: the ordered list of SQL
statements, `jobs.run_now` params, Genie PATCH bodies, the *actual principal* used,
and the set of **forbidden** calls that must NOT fire. The extraction gate: old and
extracted paths produce equivalent **results *and* effect traces**.

Known blind spots and their coverage:

| Blind spot | Coverage |
|---|---|
| Coverage already stale (suite hits a route with no decorator) | Enumerate `app.routes`; CI fails on any route without a fixture |
| Router-level mutable globals (`_live_fp_cache` keyed by principal; legacy-schema cache) | Run every fixture cold+warm, assert equality, reset globals, hoist behind a `Cache` port |
| Auth-dependent branches (`call_with_sp_fallback`, on-Apps sniff) | Matrix {OBO user, SP fallback} × {on-Apps, local}; assert *which branch fired* |
| Side effects invisible to response goldens (`/trigger`, `/apply`, `/revert`) | Effect-log golden (SQL + Job params + PATCH bodies) |
| Nondeterminism (timestamps, run_ids, floats) | Canonical scrubber by key-path allowlist; fail if a scrubbed key's type/shape changes |
| Error contracts (`HTTPException` status/detail are frontend contract) | Freeze 4xx/5xx bodies, incl. legacy-schema fallback |
| Type coercion (`_delta_accuracy_to_unit_scale` — 0.9 vs 90) | Compare parsed JSON *and* assert key-presence sets |

Use property tests only where there is a real invariant (canonicalization
idempotence, config-key-order independence in `config_fingerprint.py`). Freeze
*intended* compatibility, not known-unsafe behavior; document deliberate changes
separately.

**2. Regression-testing the nondeterministic LLM loop — never grade prose.**
- **Cassettes:** record real `_async_stream_llm` chunk streams + tool results;
  replay deterministically. Assert on (a) tool-call **name+args sequence**, (b)
  event-type sequence, (c) `config_fingerprint()` of the final config (a pure,
  SDK-free **deterministic oracle** for a nondeterministic producer), (d)
  `session.history` shape.
- **Contract-test all ~19 tools** (`handle_tool_call` is a plain dict dispatch).
- **The repair functions ARE the regression suite.** `_try_repair_json`,
  `_heal_orphaned_tool_calls`, `_repair_config`, `_recover_config_from_history`,
  `_sanitize_messages` (+ `test_compaction_400.py`) are each a scar from a real
  model misbehavior — each needs a cassette that *reproduces* the misbehavior, or
  the next refactor silently deletes the healing.
- **`_fast_create` is fully golden-testable** (no LLM) — make it the default apply
  path so nondeterminism is confined to *planning*.
- **Live-model canary — invariants, not equality.** Nightly seeded scenarios
  assert: config validates, tables ⊆ requested schemas, **zero write tools in
  plan-only mode**, rounds ≤ 15, cost < budget. Trend plan quality; alert on drift,
  don't fail per-run.

**3. Resumability & idempotency — reconcile, never replay.** For any durable
mutation the sequence is: persist pending action → obtain approval where required →
execute with an idempotency key → **persist the result before continuing**. If a
process dies after a remote create/PATCH but before recording success, mark the
action **outcome-unknown and reconcile** — never blindly replay. `SessionStore`
must never serialize credentials, SDK clients, generators, or live tasks; trusted
execution context is reconstructed each invocation.

**4. Safe rollback — flags, shadow reads only, version-pinning.**
- **Per-use-case flag read at the edge** (`WB_CORE_<usecase>=legacy|core`); the
  router keeps both paths for one release.
- **Shadow-compare reads only** (`WB_SHADOW=1` runs legacy+core, returns legacy,
  logs normalized diffs). **Never shadow writes** — `/trigger` would launch two
  Jobs; comparing LLM loops replays one recorded stream, never two live loops.
- **Pin each session/operation to its implementation version** — a flag flip must
  never route a half-completed new session into the old in-memory code. Sessions
  carry `schema_version` and the new core **refuses to load** an old format (the
  1-hour `SESSION_TTL` self-clears format-rollback risk).
- The deprecated `get_workspace_client()` reader is the **migration bridge, not a
  rollback plan** — and reverting code does **not** undo an already-applied Genie
  change. **No schema-changing migration in the same release as an extraction.**

**Release gate for headless conversational create (no partial credit):**
write-tool suppression + two-phase apply · `idempotency_key` on create/update ·
bound session identity · `turn_seq` CAS · effect journal + crash reconciliation ·
cassette coverage of the repair functions · per-surface flags · the existing web
SSE contract still passing. Minimum defensible meaning of "full conversational
create, headless and safe": **CLI and MCP each complete a multi-turn create, pause
for approval, survive reconnect, and recover an interrupted mutation without
duplicating it — while the web SSE contract still passes.**

---

## 12. Phased research roadmap (not an implementation commitment)

Create is now **full scope** and lands in P1/P2 (not deferred).

- **P0 — Extraction groundwork + safety harness.** Build the **effect-trace
  harness** across all 22 routes (route-table–driven so CI fails on any route
  without a fixture; matrixed over the OBO/SP/on-Apps identity cases; cold/warm
  cache runs). Invert the ContextVar via `run_in_context` with deprecation
  logging (it must carry identity across `ThreadPoolExecutor` submits). `git mv`
  the already-pure domain (`config_fingerprint`, pure parts of `genie_client`,
  `uc_client`) into core with `test_scanner_parity`-style **identity assertions**
  (re-export, don't re-implement). **Fix two pre-existing bugs now:** bind create
  session identity, and add `idempotency_key` to `create_space`/`update_space`.
- **P1 — Read surface + safe create (both transports).** Tier-0/1 read CLI +
  read MCP over the same core (proving the parity contract). **Headless create in
  its safe 80%:** `gwb create plan` (write tools suppressed) → `gwb create
  apply-plan` (`_fast_create`), same two tools over MCP. `WorkbenchContext` +
  `SessionStore` port; lift `run_turn` out of `event_stream`; cassette harness for
  the LLM loop. MCP *governed writes* still wait for durable operations.
- **P2 — Full conversational create + `FileLedger` + safe lifecycle.** Full
  multi-turn `create chat` (NDJSON/interactive) on CLI and the MCP turn tools;
  durable `SessionStore` with `turn_seq` CAS + effect journal + reconcile-on-crash;
  budget caps → `budget_exhausted`; typed `pending_decision`. `FileLedger`
  snapshot / restore / diff / promotion-package for no-UI teams.
- **P3 — Durable operations + governed hosting.** Unify handles under
  `Operation` over the existing Job; `DeltaLedger`; hosted MCP as a headless App
  with OBO; AI Gateway registration.
- **P4 — Promotion + publication.** promotion with approval gates;
  `gwb skills publish` → UC Skills; Supervisor integration.

**Still explicitly out of scope:** a `query_space` data-plane tool;
MCP-as-subprocess-of-CLI; hand-written UC Skills parallel to SKILL.md; a
Workbench-side authz layer; any write attributed to a service principal standing
in for a human; any **LLM-authored** write reaching Genie without a caller-signed
plan.

---

## 13. Open questions (record, don't over-debate)

1. **MCP shape for conversational create.** Resolved to full scope (§8a). One
   narrow open item: whether the one-call-per-turn model (opus — exploits the
   existing empty-message continuation) is sufficient, or whether a richer
   `operation_id` + event-cursor + optional-notifications surface (astra) is needed
   for very long executions. Start with one-turn-per-call; add async only if a
   concrete op needs it.
2. **Inline execution backend.** Whether a second `ExecutionBackend` impl is
   warranted now, or whether "inline" is mostly a test double that should be
   added only for concrete operations that need it.
3. **Promotion dependency footprint.** Confirm which promotion steps truly need
   Jobs+Delta vs which can be offline artifact work, from the *actual*
   implementation rather than tier placement.
4. **AI Gateway MCP identity propagation.** Whether registering the Workbench
   MCP in the Unity AI Gateway preserves per-user OBO identity or collapses all
   callers into one SP — if the latter, ledger attribution/approvals become
   fiction and writes over that path must be refused. **Verify early.**
5. **UC Skills publication format.** Exact packaging contract for
   `catalog.schema.skill` and how `gwb skills publish` should render a SKILL.md
   bundle into it.

## 14. Coexistence, sequencing & ready-to-execute plan (in-flight branches)

This section is the merge/sequencing strategy for landing the headless extraction
amid three other in-flight features. It is grounded in a verified collision
analysis (branch ancestry + per-file diffs against `upstream/main`), stress-tested
by both debate heads. **No file has been moved; this is a plan, not an execution.**

### 14.1 The in-flight features (verified ancestry)

| Feature | State | Relationship |
|---|---|---|
| `feature/metric-view-advisor` (MVA) | **PR open, in human testing** | Baseline of the stack. |
| `ontology` | In development, no PR | **Built on top of MVA** (MVA is its git ancestor). Beyond MVA it adds only new files (`backend/ontology/*`, ~20 files) + surgical edits to `auth.py` (+53), `routers/auth.py` (+16), `lakebase.py` (+176). |
| `version-control-cicd` (VC) | In development, **docs-only today** | **`feature/headless-cli-mcp` was cut from it** — headless already contains it. VC code (~10 modules) is almost entirely new files. |
| `feature/headless-cli-mcp` | This work | Descendant of VC. |

**Key insight: this is not three independent branches — it's a stack.** ontology =
MVA + additive; VC and headless share one lineage. `optimizer-v2` is already merged
and is not a concern.

### 14.2 Contention map — partition by file, not by branch

| File | Open-PR contention | Pre-PR contention | Verdict |
|---|---|---|---|
| `routers/auto_optimize.py` | **MVA (+2,782)** | VC (2 drift edits) | **WAIT for MVA.** The only true merge blocker. |
| `models.py` | **MVA (+735)** | ontology slice, VC | **WAIT**, then split into a `models/` package. |
| `scanner.py`, `lakebase.py` | MVA / ontology (light) | — | Wait, but trivial (append-only). |
| **`services/auth.py`** | **none** | ontology (+53), VC | **GO NOW** — highest-leverage move (§14.4). |
| **`create_agent*.py`, `routers/create.py`** | **none** | **none** | **GO NOW — fully uncontended (§14.4).** |
| `genie_client.py`, `config_fingerprint.py` | none | VC (extend in place) | Wrap-and-assert now; physical move last. |
| `main.py` | none | ontology, VC (composition) | Append-only, fine. |

**Only two files are genuinely contended** (`auto_optimize.py`, `models.py`), and
both are owned by MVA. Everything else headless needs is open road today.

### 14.3 Recommendation — do NOT wait for all three

The blend, weighted to act now:

- **(c) Influence the unmerged branches — primary play (~70%).** ontology and VC
  have no PR yet. Landing the shared `auth.py` / `WorkbenchContext` contract before
  they harden costs days now and saves a multi-week retrofit later. **This window
  closes the moment ontology opens a PR.**
- **(b) Wait-but-plan (~25%).** For `auto_optimize.py` specifically: read MVA's diff
  now, build the route-table–driven fixture/effect harness now, freeze the golden
  *values* the day MVA merges.
- **(a) Wait-for-merge (~5%).** Applies to exactly two files (`auto_optimize.py`,
  `models.py`) and to every physical `git mv`. Nothing else.

Waiting for all three would idle headless for months **and** let VC — whose code
doesn't exist yet — be written against the pre-extraction shape we are trying to
leave. That is the worst outcome on the board.

### 14.4 The two contention-driven reorderings

1. **`auth.py` / `WorkbenchContext` is the one mandatory coordination — land it
   first.** It is edited by the MVA-chain (ontology +53), by VC
   (`initiated_by` / `executed_as`), and by headless (the ContextVar inversion).
   Three independent edits to one 15-call-site ambient global produce **three
   identity models, and VC's audit ledger — whose entire value is "who did this" —
   becomes fiction.** It currently has **zero open-PR contention**, so the inversion
   can land this week, in place, with the deprecated-reader bridge, and become the
   thing ontology and VC are *written against*. **Assign a single named owner to the
   auth bridge and a prerequisite PR** — a design doc alone is insufficient (teams
   would each implement it differently). `Operation` / `operation_id` is the close
   second: VC's durable unit and headless's non-blocking MCP handle must be **one
   object**, or MCP cannot poll VC operations.

2. **Conversational-create-headless moves to the FRONT of the queue.** The entire
   create surface (`create_agent*.py`, `routers/create.py`) is **uncontended** by all
   three branches. So the biggest *new* mandated piece — the `run_turn` lift,
   `SessionStore` port, cassette harness, session↔identity binding, idempotency keys,
   two-phase safe apply — has zero merge risk and should start immediately. This
   inverts the earlier phasing (§8a/§12): create goes first *because* it is the only
   large piece of the refactor nobody else is standing on.

### 14.5 The MVA `auto_optimize.py` collision — diagnosis

A classification of MVA's ~2,782 added router lines: **15 route decorators**, ~75 new
`def`s, of which only ~6 match the classic `_build_/_derive_/_patch_` pattern — but
by content **~60 are business-derivation helpers** (`_build_semantic_graph`,
`_curated_sql_measures`, `_governed_measures_from_yamls`, `_mv_proposal_from_row`,
`_parse_join_columns`, measure synthesis, provenance indexing, YAML parsing). So the
addition is **mostly inline derivation, not thin endpoint glue.**

Consequences:
- A `routers/metric_view.py` **endpoint split captures only ~15 endpoints** — a
  smaller, optional win, not the whole fix.
- The ~60 helpers **belong in the existing `mv_*.py` service files**, but asking MVA
  to relocate them **while it is under human test is not worth the risk.**
- **Go-forward tool = the moratorium** (no *new* business logic or `_build_*`/
  `_derive_*` helpers in `auto_optimize.py`), enforced by CODEOWNERS.
- **For MVA's existing additions:** land as-is, **characterize with the effect-trace
  harness**, and lift the `mv_*` helpers into their services *after* merge, with the
  author's agreement — never as an admission price for the PR.

### 14.6 Pre-PR asks (surgical — do not refactor their features)

**To ontology (MVA-descendant, no PR yet):**
1. **Rebase onto the `auth.py` inversion**; no new `get_workspace_client()` call
   sites inside services — take an explicit client/context param at service entry.
2. Keep endpoints in `routers/ontology/*` (already doing this); add nothing to
   `auto_optimize.py`.
3. `models.py`: **append-only**, no reordering/reformatting of existing classes;
   adopt `models/ontology.py` if the split lands first.
4. `lakebase.py` (+176): put it in `lakebase_ontology.py`, don't grow the base file.
5. Merge `main` continuously so you land as **MVA-delta only** (near-free merge).

**To VC (shares core with headless, no PR yet):**
1. Import `WorkbenchContext` / `IdentityProvider` from core — **do not build a
   second identity model**; `initiated_by` / `executed_as` come from the context.
2. Use core's `Operation` / `operation_id`; `vc_operations` + the CAS row become a
   `Ledger` adapter **behind the port**, not a parallel lifecycle.
3. `config_fingerprint.py`: **additive-only in shape** — keep `canonicalize(config)
   -> dict` and the existing fingerprint signature stable; manifest/version
   fingerprints as *new* functions. Do not move the file (§14.7).
4. `genie_client.py`: no new ambient `call_with_sp_fallback`; the
   fingerprint-precondition PATCH lives in core's `GenieTransport`.
5. Its 2 `auto_optimize.py` drift edits: make them one-line delegations to core's
   drift use-case. Land VC as **10 incremental PRs**, not one mega-PR.

**To everyone, now:** CODEOWNERS on the 7 hot files + `test_scanner_parity.py`, and a
declared **"no new business logic in `auto_optimize.py`"** moratorium — justified on
`3,849 + 2,782 ≈ 6,600` lines in one router, not on unblocking a refactor.

### 14.7 config_fingerprint — wrap now, move last (do NOT `git mv` before VC lands)

VC's implementation plan references `backend/services/config_fingerprint.py` **by
path** across its module docs; moving it now would point VC's authors at a dead path.

- **Phase A (now):** nothing moves. Core wraps it:
  `domain/fingerprint.py` imports `canonicalize`, `compute_fingerprint` from
  `backend/services/config_fingerprint.py`, guarded by a `test_scanner_parity`-style
  identity assertion (`assert domain.canonicalize is config_fingerprint.canonicalize`
  + byte-identical fixtures). VC extends the file in place, additive-only.
- **Phase B (dead last, after VC modules land):** `git mv` to
  `domain/fingerprint.py`; `backend/services/config_fingerprint.py` becomes a 3-line
  re-export shim so every VC/MVA/straggler import keeps working. The parity tests
  already proved core ≡ backend, so it is a no-behavior-change PR.

Same treatment for `genie_client.py` / `scanner.py`. **`auth.py` is the exception** —
its inversion is not deferrable (VC's identity model depends on it; `run_in_context`
is the hinge), so it lands in Phase A, in place, with the deprecated-reader bridge.

### 14.8 Merge train

```
NOW  (parallel, zero conflict)
  ├─ merge version-control-cicd DOCS to main            (free, de-dups the spec)
  ├─ CODEOWNERS + auto_optimize moratorium declared
  ├─ PR-A: auth.py ContextVar inversion + core contracts ← LAND BEFORE ONTOLOGY'S PR
  ├─ PR-B: create-flow seam (run_turn, SessionStore, identity binding, cassettes)
  ├─ PR-C: workbench-core wrap-and-assert + read CLI/MCP (dark, flagged)
  └─ PR-D: fixture/effect-log harness (route-table–driven, no frozen values yet)

1. MVA merges  ── finish human testing; ask for routers/metric_view.py ONLY if cheap.
                  If the PR is imminent, take it as-is. Headless rebases onto MVA.
2. models/ package split (tiny PR, __init__ re-exports for back-compat)
3. Freeze auto_optimize goldens  ── ontology adds nothing there, so this need NOT
                                    wait for ontology
4. ontology PRs and merges        (MVA-delta only ⇒ near-free)
5. VC modules 01–10, incrementally, written on the core
6. headless Phase B: git mv + 3-line shims, one 24–48h hot-file freeze
```

**While waiting, each branch does:** MVA — finish testing, keep `models.py` appends
clean. ontology — rebase onto PR-A, keep new logic in new files, merge `main` weekly.
VC — write module specs against core's `WorkbenchContext` / `Operation` /
`GenieTransport` instead of `backend/services/*`; sequence its fingerprint module
after PR-A. headless — everything in the NOW block; **do not open a single line of
`auto_optimize.py`.**

### 14.9 What to say no to

Holding MVA's PR for a router split it doesn't want; letting ontology or VC open a PR
with its own identity handling; writing VC's 10 modules against `backend/services/*`
paths; freezing goldens before MVA merges; and any physical `git mv` before VC code
exists.
