# Coordination Ask — Version Control (version-control-cicd) Branch

**Audience:** developers/agents implementing the `version-control-cicd` modules.
**Status:** coordination request (additive choices, not a redesign).
**Why:** see `../README.md` §14 (Coexistence, sequencing & ready-to-execute plan).

---

CONTEXT: The headless Workbench effort shares one code lineage with your branch
(`feature/headless-cli-mcp` was cut from `version-control-cicd`), and both consume
the same core primitives. Your code doesn't exist yet — it's specified in
`../implementation_plan/` (under version_control) as ~10 modules — which means you
get to write it against the shared core from day one instead of being retrofitted
later. Please adopt the following as you implement. These are additive choices, not
a redesign.

1. **IDENTITY** — import `WorkbenchContext` / `IdentityProvider` from the core. Do
   NOT build a second identity model. Your `initiated_by` / `executed_as` audit
   fields come FROM the context, not from your own auth handling. (This is the single
   most important item — a parallel identity model makes your audit ledger
   unreliable.)

2. **OPERATION LIFECYCLE** — use the core's shared `Operation` / `operation_id`. Your
   `vc_operations` table + CAS coordination row should be a `Ledger` adapter BEHIND
   the shared operation port, NOT a parallel lifecycle concept. If you ship your own
   operation id independently, the MCP/CLI surfaces can't poll or observe VC
   operations, and we end up with two audit trails.

3. **CONFIG FINGERPRINT** — extend `backend/services/config_fingerprint.py` IN PLACE
   (do not move it; the headless core wraps it and will relocate it last, behind a
   compatibility shim). Constraint: keep it ADDITIVE-ONLY IN SHAPE — the existing
   `canonicalize(config) -> dict` and current fingerprint signatures must stay
   stable. Add manifest/version fingerprints as NEW functions. Everything else
   depends on the existing signatures.

4. **GENIE CLIENT** — do not add new ambient `call_with_sp_fallback` usage. The
   fingerprint-precondition PATCH (restore/write path) belongs in the core's
   `GenieTransport`; call it rather than re-implementing the write path.

5. **AUTO_OPTIMIZE DRIFT EDITS** — your 2 planned edits to `auto_optimize.py` should
   be one-line delegations to the core's drift use-case, not inline logic. That file
   is under a moratorium.

6. **MODELS** — `backend/models.py` append-only; put new VC schemas in their own
   module (e.g. `models/version_control.py`) where possible.

7. **SHIP INCREMENTALLY** — land your 10 modules as ~10 incremental PRs, not one
   mega-PR, each written on top of the core contracts.

8. **KEEP THE PLAN IN SYNC** — your `implementation_plan/` docs reference
   `backend/services/config_fingerprint.py` (and other paths) directly. If any core
   import path changes, update the affected module doc IN THE SAME PR — a stale plan
   will send an implementer to extend the wrong file even though a shim keeps imports
   working.

**WHAT WE ARE NOT ASKING:** you don't need to wait for the full core to exist before
starting additive modules, and you don't need to reorganize anything. Just write
against the core's `WorkbenchContext`, `Operation`, `GenieTransport`, and `Ledger`
contracts instead of `backend/services/*` directly, and sequence your fingerprint
module after the auth inversion PR lands.
