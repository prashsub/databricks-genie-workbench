# Platform Research — Databricks Skills, MCP, Genie Code (2026)

This document records the platform capabilities the headless-Workbench design
depends on, so the design can distinguish **what exists today** from **what we
propose to build**. Product surfaces evolve; treat version/preview status as of
this research pass (2026) and re-verify before implementation.

---

## 1. Databricks Agent Skills

**What it is.** An implementation of the open **Agent Skills** spec: a skill is a
folder with a `SKILL.md` file containing instructions/knowledge that an agent
loads on demand. Databricks maintains an official skills repository
(`databricks/databricks-agent-skills`) and a CLI for managing them
(`databricks aitools`).

**Key properties.**
- Skills are **instructions, not capability** — a skill teaches an agent *how*
  to use tools it already has (a CLI, an MCP server); it does not itself execute
  anything.
- Progressive disclosure: the agent reads a skill's metadata first, then the
  full body only when relevant.
- Distributable as files (git) and installable via CLI.

**Implication for Workbench.** Ship Workbench skills whose body references a
single authored knowledge artifact (e.g. `docs/playbooks/*`) and instructs the
agent to drive the Workbench **CLI** (coding agents) or **MCP** (hosted agents)
via the 1:1 capability contract. Author the knowledge once; do not fork it per
transport.

---

## 2. Unity Catalog Skills

**What it is (Beta, 2026).** Skills promoted to **first-class Unity Catalog
securables**, addressed as `catalog.schema.skill`. This makes a skill:
- **Governed** — standard UC grants control who can read/use it.
- **Discoverable** — listed/searchable alongside other UC objects.
- **Centrally managed** — one authoritative copy, versioned in the metastore.

**Implication for Workbench.** UC Skills are a **distribution/publication
target**, reached via a proposed `gwb skills publish`. The published UC Skill is
the *same* SKILL.md bundle rendered into a governed object — **never a second,
hand-maintained implementation**. Authorization to *use* a UC Skill (its
READ/VOLUME grants) is a **distinct layer** from MCP Service `EXECUTE` and from
the downstream Genie/UC permissions the tools ultimately exercise.

**Open question.** The exact packaging contract for `catalog.schema.skill` and
how a SKILL.md folder maps into it — to confirm at implementation time.

---

## 3. Model Context Protocol (MCP) on Databricks

### 3a. Managed MCP servers (Public Preview)
Databricks hosts managed MCP servers exposing existing platform capabilities at
fixed URL patterns under `/api/2.0/mcp/…`, governed by UC and OAuth scopes:
- **Genie** — ask natural-language questions of a Genie space (**data plane**).
- **Vector/AI Search** — query indexes.
- **Databricks SQL** — run SQL.
- **Unity Catalog Functions** — call registered UC functions as tools.

**Critical boundary for Workbench.** The managed **Genie MCP is the data
plane** (query a space). The **Workbench MCP is the control plane** (author,
scan, optimize, version, promote). **Do not duplicate** the managed Genie MCP —
no `ask` / `query_space` tools in the Workbench MCP.

### 3b. MCP Service registration in the Unity AI Gateway
Self-hosted / external MCP servers can be **registered as an MCP Service** in
the Unity AI Gateway, bringing them under UC governance:
- Grant `EXECUTE` on the service to control who may call it.
- Restrict which tools the service exposes.

**Implication for Workbench.** The hosted Workbench MCP server (a headless App)
would be registered here. **Verify early** whether registration preserves
per-user **OBO identity** or collapses callers into a single service principal —
this determines whether ledger attribution and human-approval semantics survive
over the gateway path. If identity collapses, **writes over that path must be
refused** (automation may propose/observe; only a human OBO identity may
approve/release/promote).

---

## 4. Genie Code / Agent Bricks Supervisor

**What it is.** Agent Bricks **Supervisor Agents** orchestrate/route across
multiple capabilities — Genie spaces, UC functions, MCP servers, Knowledge
Assistants — to answer multi-part requests. "Genie Code" style server-side
agents cannot shell out to a CLI.

**Implication for Workbench.** A Supervisor would route to the Workbench **MCP**
tools (not the CLI). Expose a **read-only / assessment subset** to general
supervisors by default; keep mutation tools on a separately-permissioned release
surface. This is precisely why the CLI-only-for-skills stance was rejected in
the debate — hosted agents are core consumers and need MCP.

---

## 5. Auth & governance building blocks

- **OAuth / OBO (on-behalf-of-user).** Databricks Apps run tool calls under the
  invoking user's identity where scopes allow — the basis for honest ledger
  attribution.
- **Service principals & M2M OAuth.** For automated/headless execution;
  least-privilege, per-workspace.
- **Three distinct authorization layers** the design must keep separate:
  1. UC Skill access (READ / VOLUME grants).
  2. MCP Service `EXECUTE` grant in the AI Gateway.
  3. Downstream Genie space / UC table / warehouse permissions the tools use.
- **`call_with_sp_fallback` (this repo).** Fires **only** on OAuth *scope*
  errors (not permission denials); runs the call under the SP identity and logs
  a WARNING. Must remain **read-only and scope-only**; never extended to writes.

---

## 6. Repository facts that constrain the design (verified in code)

| Fact | Evidence | Consequence |
|---|---|---|
| Ambient identity via `ContextVar` | `get_workspace_client()` at 15+ sites incl. `scanner.py:168`; `run_in_context` exists | Must invert (not delete) the ContextVar; it is the migration hinge. |
| Business logic in router | `auto_optimize.py` ≈ 3,849 lines, 40+ `_build_*/_derive_*` helpers | Real extraction cost lives here, not in the domain. |
| Process-bound create | SSE `StreamingResponse` + in-memory `create_agent_session.py` (+ ~3,446 lines of create tools) | Conversational create is not headless-able by injection; defer. |
| Pure domain already | `config_fingerprint.py` = 261 lines, zero infra imports | Domain move is a `git mv`; don't over-credit it. |
| No local execution | Delta via `sql_warehouse_query(ws, warehouse_id, sql)`; no databricks-connect | "Local" = no Job/Lakebase, never no-Databricks; no local optimizer. |
| Optimizer is a Job with apply | `GSO_JOB_ID`; returns `run_id`/`job_url`; `apply_mode` exists | Propose-only is policy, not a capability gap; unify handles under `Operation`. |
| Scope-only SP fallback | `call_with_sp_fallback` in `genie_client.py` matches `insufficient_scope` only | Substitution is read-only; forbid on writes and on denials. |

---

## 7. Sources to re-verify at implementation time

Because these are preview/beta and fast-moving, confirm against current
Databricks documentation before building:

1. Agent Skills spec + `databricks aitools` CLI surface and the
   `databricks/databricks-agent-skills` repo layout.
2. Unity Catalog Skills packaging contract (`catalog.schema.skill`), grants, and
   publication mechanics.
3. Managed MCP server URL patterns, OAuth scopes, and the exact tool schemas of
   the managed Genie MCP (to avoid overlap).
4. MCP Service registration in the Unity AI Gateway: identity propagation
   (OBO vs SP), `EXECUTE` grant semantics, tool-exposure restrictions.
5. Agent Bricks Supervisor routing model and how it enumerates/selects MCP
   tools.
