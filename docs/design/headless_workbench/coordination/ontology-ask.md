# Coordination Ask — Ontology Branch

**Audience:** developers/agents building the `ontology` branch.
**Status:** coordination request (not a mandate to refactor the feature).
**Why:** see `../README.md` §14 (Coexistence, sequencing & ready-to-execute plan).

---

CONTEXT: A headless Workbench effort (CLI + MCP) is extracting shared primitives
into a coordinated core. Your branch (`ontology`) is built on top of
`feature/metric-view-advisor` and edits some of the same shared files. We are NOT
asking you to refactor your feature or delay it — we're asking for a few small
contract-alignment choices as you develop, so nothing has to be painfully unwound
later. Keep building your ontology feature as planned; just adopt the following.

1. **IDENTITY / AUTH — the one that matters most.**
   A shared `WorkbenchContext` + explicit identity model is landing in `auth.py`
   (the ContextVar inversion, with `run_in_context` as the bridge and a deprecated
   reader for old call sites). When it lands:
   - Do NOT add new `get_workspace_client()` call sites inside services. Take an
     explicit client/context parameter at service entry; let the router resolve it.
   - Do NOT introduce your own identity/service-principal-fallback handling. Consume
     the shared bridge. There must be exactly one identity model across features, or
     the audit trail (who did what) becomes unreliable.
   - If you need a new accessor, make it one function in one place, reachable from
     the context — not a new ambient global.
   ACTION: rebase your `auth.py` (+53) changes onto the shared inversion PR once it's
   available; until then, keep your auth edits minimal and flag them to the auth
   owner rather than expanding them.

2. **ROUTERS** — keep your endpoints in `backend/ontology/routers/*` (you already do
   this — thank you). Add NOTHING to `backend/routers/auto_optimize.py`. That file is
   under a moratorium (no new business logic) because it's already ~6,600 lines
   post-MVA.

3. **MODELS** — `backend/models.py` must be treated as APPEND-ONLY. Do not reorder or
   reformat existing classes (it keeps git merges trivial). If a `models/` package
   split lands, move your additions into `models/ontology.py`.

4. **LAKEBASE** — put your `lakebase.py` additions (+176) in a separate
   `lakebase_ontology.py` module instead of growing the shared base file.

5. **STAY MERGE-CHEAP** — merge `main` continuously so you always land as an
   "MVA-delta only" change. With MVA as your ancestor, your merge is near-free if you
   don't let it drift.

**WHAT WE ARE NOT ASKING:** don't reorganize existing schemas, don't clean up
unrelated imports, don't restructure your services. Those reconcile fine at merge.
The only time-sensitive item is #1 (identity) — please coordinate with the auth
bridge owner BEFORE you open your PR.
