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
