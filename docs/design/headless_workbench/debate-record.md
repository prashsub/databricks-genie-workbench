# Debate Record — Headless Workbench (CLI + MCP)

A code-rooted brainstorming debate between two heterogeneous models, run through
Debby. This records the positions and their movement so the design decisions in
`README.md` are traceable. Brainstorming artifact — no code was written.

## Participants

| Head | Harness | Model | Grounding |
|---|---|---|---|
| 🟠 Claude (opus) | claude-sdk | `claude-opus-4-6` | Read the actual repository code. |
| 🔵 GPT (astra) | Codex | `databricks-gpt-6-astra` | Opening from file inventory; Round 1 onward rooted in a verified code fact sheet. |

Genuinely heterogeneous — two distinct providers, no substitution. (The Claude
head was requested as Opus 5 but the gateway served Opus 4.6; recorded honestly.)

## Method

- **Round 0 — opening positions**, each head answering the headless-Workbench
  brief independently.
- **Code reconnaissance** — a verified fact sheet extracted from the repo
  (ambient auth, router mass, SSE create sessions, pure domain, warehouse-only
  execution, optimizer-as-Job, scope-only SP fallback).
- **Round 1 — cross-critique**, both heads arguing from the same code facts.
- **Convergence** — synthesized into `README.md`; a last factual question
  (`call_with_sp_fallback`) resolved directly from code.

---

## Round 0 — opening positions

**🟠 Claude (opus).** "The refactor *is* the project." Anchored on hard code
facts: `auto_optimize.py` at 3,849 lines with logic in the router; ambient
`ContextVar` auth as the #1 headless blocker; the create flow as an SSE/LLM loop
that should not be an MCP tool. Proposed `workbench-core` + three thin adapters;
managed Genie MCP = data plane vs Workbench MCP = control plane; skills call the
CLI; every write tool carries `expected_fingerprint` + `idempotency_key`. Flagged
the AI-Gateway identity-collapse risk as the sharpest governance threat.

**🔵 GPT (astra).** "Make Workbench a Genie *lifecycle engine*, not an App with
remote controls." Clean hexagonal layering (domain / application / infrastructure
/ interfaces); injectable version storage + execution; MCP as lifecycle
management, not another Genie chat server; `optimize` yields a candidate not a
live mutation; three authorization layers kept distinct; one pip package with
`[cli]/[mcp]/[app]` extras.

**Consensus at opening:** one headless core with thin adapters; Workbench MCP is
the control plane and must not duplicate the managed Genie MCP; optimize is
propose-first; long ops return handles; skills are instructions not capability;
identity must stay honest across surfaces.

---

## Round 1 — cross-critique (code-rooted)

**🟠 opus → astra.** Conceded "skills call the CLI, period" was too absolutist
and adopted astra's CLI-first-coding / MCP-first-hosted split — adding the 1:1
parity contract (identical names/params/JSON verified in CI) and the
transport-absence-vs-authz-denial distinction. Adopted astra's injectable
`Ledger` (`DeltaLedger` + `FileLedger`) with a "FileLedger refuses
concurrency/approval" caveat. Pressed that astra **under-weights the mass**
(~7.3k lines of real untangling vs a 1-line domain rename) and named the
`run_in_context` inversion + capability-field levers. Rejected "use-cases own
authz" (no second policy surface). Reframed tiers as three; kept `apply_mode` in
core but split the MCP surface `start→get→apply`; unified handles under
`Operation`.

**🔵 astra → opus.** "This is an extraction project, not a packaging project" —
a clean reversal toward opus's thesis. Concessions: extraction mass (staged
enforcement — zero `services.auth` imports in *extracted* modules, not the whole
tree); platform-delegated authz (retaining only Workbench *execution policy*);
no local optimizer; the shared `Operation` identity; the create workstream is
separate. Holds where the code supports it:
- **Parity:** accept the capability contract, **reject byte-identical JSON** —
  test identical *structured results under fixed fixtures* (envelopes/ids/times
  differ).
- **FileLedger:** "cannot implement CAS" is too absolute — a file artifact **can
  feed a PR approval workflow**, it just can't *enforce* approval or coordinate
  deployment.
- **Tiers:** prefer **per-capability availability** (`missing GSO_JOB_ID`) over a
  coarse `TierUnavailable`; "exactly two ExecutionBackends" is premature if inline
  is mostly a test double.
- Flagged that opus's "OBO can PATCH or it can't" is incomplete given
  `call_with_sp_fallback` — substitution genuinely exists and must be made
  explicit.

---

## Round 2 — regression stress-test under full-scope create

The user (a) **rejected deferring conversational create** — it must be reachable on
both CLI and MCP — and (b) asked both heads to stress-test whether the refactor is
regression-ridden given its scale.

**🟠 opus.** Re-read the create-flow code and materially reversed itself: the seam
it earlier said "didn't exist" is already there. `create_agent.chat()` is already a
transport-agnostic async generator (SSE coupling ≈ 4 lines); the turn boundary is
already externalized (`needs_continuation`, `MAX_TOOL_ROUNDS=15`), so **MCP needs no
streaming — one call = one turn**; a deterministic `_fast_create` apply path already
exists; the session store is already half-built (L1 memory + L2 Lakebase). Safety
falls out of existing code: run the loop with `create_space`/`update_space`
suppressed → caller signs a plan → deterministic apply. On regression: golden JSON
is necessary but insufficient (7 evidenced blind spots — stale route coverage,
principal-keyed mutable caches, auth-branch matrix, **invisible side effects → add an
effect-log golden**, scrubber-hidden nondeterminism, error contracts, type
coercion); template = **re-export + identity assertion** (the repo's own
`test_scanner_parity.py`); LLM loop via **cassettes** freezing tool-call sequence +
`config_fingerprint()` oracle, never prose; **"the repair functions ARE the
regression suite."** Rollback: per-use-case edge flags, **shadow reads only**,
deprecated-reader bridge, session `schema_version` refusal, no migration+extraction
in one release. Flagged two pre-existing bugs to fix in P0: no identity check in
`get_session_async()`, and duplicate-create-on-timeout (idempotency key required).
Corrected its own tier table: **create is Tier 1**, not Tier 2.

**🔵 astra.** Conceded create is now a release requirement on both surfaces, but
"extract the existing behavior, don't rewrite the 3,446-line tool loop." Golden
responses are "necessary and dangerously incomplete" → freeze an **effect trace**
(ordered SDK/model/storage calls + actual principal + forbidden calls). Seam =
transport-independent **driver + `SessionStore` port**, batch-create only as a
convenience over the same loop; capabilities `start/submit_message/get(cursor)/
resolve_approval/cancel`, turns serialized by an **expected session revision**,
deduped by request id. Non-negotiable: an **effect journal** — persist pending →
approve → execute-with-idempotency → persist result before continuing; crash after
remote create but before recording = **outcome-unknown → reconcile, never replay**;
never serialize creds/clients/generators. LLM testing: scripted fake model +
sanitized record/replay + per-tool contracts; live models judged on invariants, not
prose. Rollback: **pin each session/operation to its implementation version**;
shadow pure derivations only, never live create/PATCH; "the deprecated auth reader
is a migration bridge, not a rollback plan — reverting code doesn't undo an applied
Genie change."

**Convergence:** create moves onto the critical path and is headless-able by
extracting a transport-independent driver over the *existing* turn protocol + a
`SessionStore` port (not a rewrite, not a parallel batch path); freeze **effect
traces**, not just JSON; test the LLM loop by freezing the tool-call sequence + a
config fingerprint (never prose); two-phase safe create (plan with writes
suppressed → caller-approved apply); **idempotency + reconcile-never-replay**;
rollback via per-surface flags, shadow-reads-only, version-pinning, and no
migration+extraction in the same release.

**Narrow residual differences:** (1) MCP create shape — opus's one-call-per-turn
vs astra's richer operation_id+cursor(+optional notifications) for long runs;
(2) how much is already "free" (opus quantified it from code); (3) create's tier
(opus corrected to Tier 1, vindicating astra's "local foreground" framing for
create specifically).

## Converged decisions (now in README.md)

1. Extraction, not packaging, is the project.
2. One `workbench-core`; CLI/MCP/App are thin adapters; none wraps another.
3. Invert the `ContextVar` via `run_in_context`; ban new ambient call sites;
   assert zero `services.auth` imports in extracted core; replace the env-sniff
   with an explicit capability field.
4. Injectable `Ledger` — `DeltaLedger` (governed) + `FileLedger` (git-friendly,
   for no-UI teams). FileLedger stores/feeds-PR but cannot enforce/coordinate.
5. No Workbench-side authz layer — delegate to the platform; own audit +
   preconditions + coarse execution policy only.
6. `call_with_sp_fallback` is scope-only + read-only; forbidden on writes and on
   denials; record actual execution identity.
7. 1:1 capability parity, tested as identical **structured results under fixed
   fixtures** (not byte-identical), through both transports in CI.
8. Workbench MCP = control plane; no `ask`/`query_space`; long ops return
   `operation_id`; optimize propose-only by default (`start→get→apply`),
   `apply_mode` retained in core.
9. One `Operation` identity shared with the version-control design; Jobs handle
   under `external_ref`; reuse Jobs, don't build a scheduler.
10. Per-capability `describe_environment`; no production local optimizer.
11. Skills = instructions; UC Skills = governed publication of the same bundle;
    fallback on transport absence but never after an authorization denial.

## Remaining disagreements (recorded, not blocking)

1. **Parity strictness** — byte-identical (opus) vs structured-result-identical
   (astra). *Design adopts astra's.*
2. **FileLedger scope** — refuses all approval flows (opus) vs can feed a PR
   approval workflow but not enforce it (astra). *Design adopts astra's nuance.*
3. **Environment reporting** — three tiers + `TierUnavailable` (opus) vs
   per-capability availability (astra). *Design adopts astra's, tiers as a
   summary.*
4. **ExecutionBackend count** — two impls now (opus) vs add inline only when a
   concrete op needs it (astra). *Open.*
5. **Create extraction seam** — deterministic `create_agent_from_config` is
   viable; the conversational SSE author is last/never headless. *Open.*

## Round 2 additions to converged decisions

12. Conversational create is **full scope** on CLI and MCP, extracted over the
    existing turn protocol (§8a); no LLM-authored write reaches Genie without a
    caller-signed plan (two-phase: suppress write tools → approve plan →
    `_fast_create`).
13. Regression safety = **effect-trace goldens** (not just JSON) + **re-export
    with identity assertion** + **cassettes/invariants** for the LLM loop +
    **idempotency & reconcile-never-replay** + **shadow-reads-only / version-pinned**
    rollback. Fix two pre-existing bugs first: create session identity binding and
    create idempotency keys.

