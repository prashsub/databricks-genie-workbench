# M10 (Optimizer Integration) — Cross-Vendor Review Fixspec

**Verdict: BLOCK** (5 blocking findings, 10 non-blocking observations)

- **Reviewed artifact:** `.polly/m10.diff` (2197 lines, 19 files), read in full.
- **Repository state at review:** `HEAD = b0b8411`, working tree clean — the diff is
  **unapplied**. All findings below are therefore derived from (a) the diff text and
  (b) the actual `HEAD` sources the diff would land against.
- **Implementer vendor:** GPT/Astra. **Reviewer vendor:** Claude (independent).
- **Authoritative contract:** VC/1.0 —
  `docs/design/version_control/implementation_plan/module-10-optimizer-integration.md`,
  `contracts.md`, `testing-strategy.md`, `packages/genie-space-optimizer/AGENTS.md`.
- **Evidence method:** diff text + `HEAD` source reading, plus executable probes run
  against the repo venv (`.venv/bin/python`) reconstructing the diff's own classes
  verbatim in a scratch dir (`/tmp/m10probe/`). **No file in the implementer's
  worktree was modified.** A scratch *copy* of the repo was attempted and denied by
  the sandbox classifier, so B4/B5 rest on source reading rather than execution;
  B1/B2/B3 are execution-proved.

---

## 1. What M10 got right

This is a substantially well-designed change. Credit where due:

1. **Candidate isolation is a genuine positive proof, not a caller boolean.**
   `assert_candidate_write` (diff L869-874) requires `isinstance(client, _EvaluationClient)`
   and then re-validates ownership through the registry. There is no
   `trust_me=True` parameter anywhere. This is exactly what module doc §3 demanded
   ("Candidate evaluation takes an `EvaluationBinding` positively proven not equal
   to a managed binding; isolation cannot be a caller boolean"). I probed it
   adversarially (see §3, obs. 8) and the boundary held.

2. **The real `revert.py` was located and actually retired — not left as a live bypass.**
   Module doc §8 explicitly warned "Locate actual `revert.py` and all optimizer
   mutation utilities; this path is deliberately not invented." The implementer found
   `packages/genie-space-optimizer/src/genie_space_optimizer/integration/revert.py`
   (the correct, real path), gutted `revert_optimization` **and** the private
   `_apply_revert_state` compensation helper, removed the `rollback(...)` call from
   `discard.py`, and stubbed `apply.py`. I verified by grep that no in-process
   managed-state PATCH survives on any of those three paths. F-COMPENSATION is met
   for the paths the diff touches.

3. **At-most-one-optimizer-version-per-run is correct end-to-end.**
   The idempotency key (diff L104-107) is
   `hash(run_id, binding_id, binding_revision, workspace_id)` — a stable run+binding-revision
   scope exactly as §3/§8 require. `test_retry_or_second_champion_in_same_run_cannot_append_second_optimizer_version`
   (diff L222-252) restarts the adapter under a *new* `execution_ref` to simulate a new
   Job attempt and asserts `len(durable_requests) == len(versions) == 1` while
   `gate.execute.call_count == 2`. That correctly encodes §8's "A retry with a new
   execution attempt is not a new authorization; run-level at-most-one semantics
   outlive coordination row reuse." This invariant is the one the diff nails.

4. **The packaging/entry-point test is a real RED, not a smoke test.**
   `test_package_install_exposes_shared_gate_without_importing_backend_app`
   (diff L1348-1384) spawns an isolated `sys.executable -I` subprocess, installs a
   `MetaPathFinder` that **raises** on any `backend`/`backend.*`/`fastapi` import,
   loads the real installed entry point via `importlib.metadata`, asserts
   `contract_version == "VC/1.0"`, drives a champion apply through the injected
   adapter, and re-checks `"backend.main" not in sys.modules`. This fails if the
   guarantee is reverted. Task 13 is genuinely satisfied on the package side.

5. **Integration-test honesty is the best-handled part of the diff.** See §5 —
   module-level marker, `pytest.fail` (not `skip`) on missing config, and an
   explicit written "**not platform-validated** … remains a deployment blocker."

6. **Owned-paths discipline is clean.** `databricks.yml` untouched; nothing outside
   the manifest. See §4.

7. **The `ChampionRun` unresolved-outcome latch is correct.** diff L940-946: a second
   `apply` with identical identity and `receipt is None` raises
   `RuntimeError("Unknown apply outcome requires governed Job recovery")` rather than
   retrying, and `test_unknown_adapter_exception_latches_against_in_process_retry`
   proves the `TimeoutError` case latches. FM-RECOVERY's "ambiguous apply never
   blindly replays/reverts" holds at this seam.

The blockers below are concentrated in one place: **the adapter was written against
mocks of M06/M08 and never bound to the real ones**, so three contract mismatches
(B1, B2, B3) survive a fully green 7-test suite. B4/B5 are coverage gaps in the
isolation sweep.

---

## 2. Blocking findings

Each finding states: location → invariant → why it's a bug (with evidence) → required
fix → named RED test → **ownership** (M10-own-paths vs. integrator-sanctioned
cross-module change).

---

### B1 — Adapter gates on a feature flag that does not exist; champion apply raises `KeyError` the instant writes are enabled

**Location**
- `backend/services/version_control/optimizer_adapter.py:81-83` (diff L81-83).
- Colliding authority: `backend/services/version_control/platform/feature_flags.py:19-25, 36-38` (at `HEAD`, M08-owned).

```python
# optimizer_adapter.py:81-83  (diff L81-83)
if self.flags is None or any(self.flags.enabled(name) is not True for name in (
    "vc_writes_enabled", "vc_optimizer_apply_enabled", "vc_optimizer_contract_ready",
)):
    raise PermissionError("Optimizer champion writes are disabled")
```

**Invariant / contract violated**
- `testing-strategy.md` §Definition of done: "disabled paths **visibly refuse** writes."
  An unhandled `KeyError` is not a visible refusal.
- Module doc §6 FM-OUTAGE acceptance: "optional telemetry fallback cannot bypass gate"
  — the switch ladder must fail *closed and cleanly*, not crash.
- `contracts.md` §preamble + module doc §7: "**M08 alone changes composition and
  dependencies**" / "M08 alone changes optimizer DAB manifest and root package
  dependencies." M10 invented a flag in M08's registry.

**Why it's a bug (execution evidence)**
`FeatureFlags.enabled()` is an **unguarded dict lookup**:
```python
# feature_flags.py:36-38
def enabled(self, name: str) -> bool:
    requested = self.registry[name]          # <-- KeyError on unknown name
    return requested and (name not in WRITE_SWITCHES or self.vc_writes_enabled)
```
`FeatureFlags` declares exactly six fields: `vc_writes_enabled`, `vc_restore_enabled`,
`vc_promotion_enabled`, `vc_optimizer_apply_enabled`, `vc_reconcile_enabled`,
`vc_history_enabled`. **`vc_optimizer_contract_ready` is not among them.**

Probe run against the real M08 class:
```
$ .venv/bin/python -c "<construct FeatureFlags, call adapter's any(...) ladder>"
all-off                                 -> any() = True      # bug masked
writes=True, optimizer_apply=True       -> RAISES KeyError 'vc_optimizer_contract_ready'
```
The all-off case masks the defect because Python's `any()` short-circuits on the
**first** name (`vc_writes_enabled` → `False` → `is not True` → `True`) and never
evaluates the third. The bug is therefore *invisible until the feature is switched
on*, at which point the champion apply path is permanently unenablable.

Why no test catches it: `test_optimizer_write_switches_default_off` (diff L214-220)
sets `adapter.flags = None`, short-circuiting before the loop entirely; and the `rig`
fixture uses `flags = Mock(); flags.enabled.return_value = True` (diff L173-174),
which happily returns `True` for **any** string including a nonexistent flag. **No
test in the diff ever constructs a real `FeatureFlags`.**

**Required fix**
Drop `"vc_optimizer_contract_ready"` from the tuple at `optimizer_adapter.py:82`.
The two remaining names are sufficient and correct: `M04`'s own gate already
re-checks both (`mutation_gate.py:141-149` maps `optimizer_apply` →
`vc_optimizer_apply_enabled`), and `FeatureFlags.enabled` already ANDs every
`WRITE_SWITCHES` member with `vc_writes_enabled`, so the ladder is defence-in-depth,
not a missing gate. A third "contract ready" switch adds no safety the other two
don't already provide.

**RED test** — `backend/tests/test_vc_optimizer_adapter.py::test_adapter_uses_only_m08_declared_switches`
- **Seeds:** the real `backend.services.version_control.platform.feature_flags.FeatureFlags`
  (not a `Mock`) in two configurations — `FeatureFlags()` and
  `FeatureFlags(vc_writes_enabled=True, vc_optimizer_apply_enabled=True)` — with the
  rest of the `rig` unchanged.
- **Asserts:** with all-off, `pytest.raises(PermissionError)` and
  `gate.execute.assert_not_called()`; with both switches on, `apply(...)` reaches
  `gate.execute` and returns its result. Additionally assert every name the adapter
  passes to `enabled()` is a member of `FeatureFlags(...).registry` (introspect
  `flags.enabled.call_args_list` against the registry keys) so a future invented flag
  is caught by name rather than by crash.
- **Fails today:** the enabled configuration raises `KeyError`, not `PermissionError`,
  and never reaches `gate.execute`. Reverting the fix re-reddens it.

**Ownership: M10's own paths.** The fix belongs in the adapter
(`backend/services/version_control/optimizer_adapter.py`, M10-owned) — delete the
invented flag name. This does **not** require an M08 change and should not be
"fixed" by asking M08 to add a `vc_optimizer_contract_ready` field: that would
expand M08's write-switch surface to satisfy an M10 implementation detail, and per
module doc §7 M08 alone owns that registry. Adding the field is the wrong direction;
dropping the name is a one-line M10 fix. **No integrator sanction needed.**

---

### B2 — `request_digest` is computed under an M10-private hash domain, so the real M06 `authorize()` rejects every champion request

**Location**
- `backend/services/version_control/optimizer_adapter.py:103` (diff L103).
- Colliding authority: `backend/services/version_control/governance/approvals.py:26-29`
  (`request_digest`) and `:59-76` (`_bound`), both at `HEAD`, M06-owned.

```python
# optimizer_adapter.py:103  (diff L103)
digest = vc.canonical_json_hash("vc-optimizer-request/1", vc.to_wire(source))
...
request = ChampionMutationRequest(
    vc.RequestIdentity(str(uuid5(...)), key, digest),   # <-- digest lands in identity.request_digest
    ...
)
```

**Invariant / contract violated**
- Module doc §5 task 12 (`test_job_executor_verifies_requester_edit_or_release_policy`)
  → "M06 grant check; no SP confused-deputy bypass."
- Module doc §6 acceptance: champion apply is gated by M06 grant/approval.
- `contracts.md` §1: "Hash versioned, deterministic UTF-8 JSON" — the *domain* is part
  of the contract, and M06 owns the `vc-request/1` domain for request digests.
- `testing-strategy.md` §Red→green step 4: "Before integration, run **consumer/provider
  contract fixtures**." This defect is precisely what that step exists to catch.

**Why it's a bug (execution evidence)**
M06's `_bound()` — called on **every** `authorize()` — hard-requires:
```python
# approvals.py:63
if (request.identity.request_digest != request_digest(request)
        or ...):
    raise ValueError('Request is not bound to reviewed immutable inputs')
```
where M06's canonical digest is a *different domain over a different payload*:
```python
# approvals.py:26-29
def request_digest(request):
    payload = to_wire(request)
    payload['identity'] = {'operation_id': request.identity.operation_id}
    return canonical_json_hash('vc-request/1', payload)
```
M06 hashes the **request** (identity reduced to `operation_id`) under `vc-request/1`.
The adapter hashes the **source artifact** under `vc-optimizer-request/1`. These can
never coincide.

Probe — I reconstructed the exact `ChampionArtifact` / `ChampionMutationRequest` the
diff builds (verbatim field order confirmed:
`['identity','binding','operation_type','source_version_id','expected_base','serialized_space','description','approval_id','origin','optimizer_run_id','champion_id','source_payload_digest']`)
and compared both digests:
```
adapter identity.request_digest : 2e57cd85097fe83720ef...
M06 request_digest(request)     : 586d81c82f4f438c8a0e...
MATCH (M06 _bound requirement)  : False
```
So with the real `ApprovalService`, `authorize()` raises
`ValueError('Immutable request identity or digest mismatch')` at `approvals.py:182-183`
(or `'Request is not bound to reviewed immutable inputs'` at `:70`) on **100% of
champion applies**. The M06 gate is claimed but structurally unreachable.

Note this is *not* merely cosmetic: the adapter's `source_payload_digest` field
already exists (diff L42, and the source-digest integrity check at diff L92 is
correct and valuable). The source digest belongs there — it does not belong in
`identity.request_digest`, which is M06's binding anchor.

Why no test catches it: `approvals = Mock(spec=vc.ApprovalService)` with
`authorize.side_effect = lambda request, job: vc.AuthorizationGrant(...)`
(diff L177-182) fabricates a grant from whatever request it is handed. A `Mock(spec=...)`
validates the *call signature*, never the *digest contract*.

**Required fix**
Use M06's canonical request digest as `identity.request_digest`. Either import
`request_digest` from `backend.services.version_control.governance.approvals`, or
(cleaner, and preferable for the seam) ask M02 to export it from `contracts.py`
alongside `canonical_json_hash` so M10 does not import an M06 implementation module.
Keep `source.payload_digest` where it is, in `source_payload_digest` (diff L42), and
keep the diff L92 integrity check unchanged.

**RED test** — `backend/tests/test_vc_optimizer_adapter.py::test_champion_request_digest_satisfies_m06_binding`
- **Seeds:** the `rig` fixture unchanged, but capture the `ChampionMutationRequest`
  the adapter hands to `gate.execute` (or to `approvals.authorize`).
- **Asserts:** `request.identity.request_digest == request_digest(request)` using the
  **real** M06/M02 `request_digest` function. Also assert
  `request.source_payload_digest == source_digest(source.serialized_space, source.description)`
  so the two digests are pinned as distinct-and-both-present.
- **Fails today:** the equality is `False` (proved above, digests diverge in the first
  byte). Reverting the fix re-reddens it immediately.

**Ownership: M10's own paths, with one optional M02 courtesy.** The fix is a one-line
change in `optimizer_adapter.py` (M10-owned) to call the correct digest function.
Importing it from M06's `governance/approvals.py` needs **no** cross-module edit and
is legal today (read-only dependency on M06's requester policy is explicitly listed
in module doc §4). If the team prefers M10 not to import an M06 module, re-exporting
`request_digest` from M02's `contracts.py` is a **1-line additive M02 change requiring
integrator sanction** — but that is a hygiene preference, not a requirement. **M10 can
land this fix unilaterally.**

---

### B3 — The adapter's own grant validation contradicts M06's grant construction, so **both** M06 authorization branches are dead for `operation_type='optimizer_apply'`

**Location**
- `backend/services/version_control/optimizer_adapter.py:99-102` (dev self-service branch, diff L99-102).
- `backend/services/version_control/optimizer_adapter.py:121-130` (grant validation, diff L121-130).
- Colliding authority: `backend/services/version_control/governance/approvals.py:53-57`
  (`get`), `:174-214` (`_authorize`), all at `HEAD`, M06-owned.

```python
# optimizer_adapter.py:99-102  (diff L99-102)
if source.approval_id is None:
    if (binding.environment != "dev"
            or self.identity.can_edit(source.requester_id, binding) is not True):
        raise PermissionError("Requester edit right or approved release policy is required")
...
# optimizer_adapter.py:121-130  (diff L121-130)
grant = self.approvals.authorize(request, executor)
if (... or grant.approval_id != source.approval_id):
    raise PermissionError("M06 grant is stale or not bound to champion request")
```

**Invariant / contract violated**
- Module doc §5 task 12 → "M06 grant check; no SP confused-deputy bypass."
- Module doc §6 acceptance: "champion apply is gated by M06 grant/approval."
- `contracts.md` §6: "Self-service edits use an explicit rights-checked grant; they are
  **not** implicit approvals."
- F-IDENTITY (`testing-strategy.md` failure matrix): "SP acting for unauthorized
  requester → rights/policy checked server-side."

**Why it's a bug (source evidence)**
Trace both M06 branches with `operation_type = "optimizer_apply"`:

*Branch A — rights-based self-service grant* (`approvals.py:191-198`):
```python
if inputs.target_binding.environment == 'dev' and inputs.operation_type == 'edit':
    ...
    return AuthorizationGrant(..., None, None)     # approval_id=None
```
Gated on `operation_type == 'edit'`. The champion request carries
`operation_type="optimizer_apply"` (diff L110), so **this branch is never entered.**

*Branch B — two-human-approver grant* (`approvals.py:199-214`): reached by fallthrough.
But `_authorize` begins at `approvals.py:176` with `record = self.get(request.approval_id)`,
and on the self-service path `request.approval_id is None`, so:
```python
# approvals.py:53-57
def get(self, approval_id):
    record = self.facts.get_request(approval_id).approval
    if record is None or record.request.approval_id != approval_id:
        raise LookupError('Approval evidence unavailable')
```
→ `LookupError`. **Branch B is also dead** for the `approval_id is None` case.

So the adapter's dev/self-service path (diff L99-102) can never obtain a grant: the
branch it *needs* is `operation_type`-gated to `'edit'`, and the fallback raises.
And on the `approval_id is not None` path, B2 already blocks at `_bound`. Every route
through M06 fails, which means the M06 gate the acceptance criteria demand is
**asserted only against `Mock(spec=vc.ApprovalService)` and exists nowhere in
composition.**

Secondary note (correct, but currently unreachable): the adapter's re-validation of
`grant.rendered_target_digest` against a locally recomputed
`canonical_json_hash("vc-rendered-target/1", {...})` (diff L126-128) uses the **right**
domain — it matches `approvals.py:60-62` exactly. That check is good defence-in-depth
and should be kept; it simply never executes today.

Also worth flagging: `test_release_policy_denial_and_wrong_job_run_as_fail_closed`
(diff L276-289) *appears* to cover the prod/approval path, but it only asserts that a
`Mock` configured to raise `PermissionError("release policy revoked")` does in fact
propagate. That is a tautology over the mock's own `side_effect`, not evidence about
M06.

**Required fix**
Decide and encode which M06 branch authorizes `optimizer_apply`, then make the
adapter's validation agree with it. Two defensible options:

1. **Narrow (M10-only, recommended for this milestone):** require a real
   `approval_id` unconditionally. Delete the dev self-service branch at
   `optimizer_adapter.py:99-102` and require `source.approval_id is not None`,
   raising `PermissionError` otherwise. Champion apply on a managed binding is a
   governed live mutation; requiring an approval is the conservative reading of
   `contracts.md` §6 ("Production: two distinct human approvers"). Combined with the
   B2 fix, M06 Branch B then works.
2. **Broader (needs M06):** add `'optimizer_apply'` to M06's rights-eligible
   operation types at `approvals.py:191` so dev self-service yields a rights grant.
   This changes M06's authorization policy surface.

Whichever is chosen, `grant.approval_id != source.approval_id` (diff L129) must be
consistent with the branch actually taken.

**RED test** — `backend/tests/test_vc_optimizer_adapter.py::test_champion_apply_authorizes_through_real_approval_service`
- **Seeds:** the **real** `ApprovalService` (`governance/approvals.py`) wired to the
  M02-owned fake fact store (`backend/tests/vc_fakes/stores.py`) and a controllable
  UTC clock, with `writes_enabled=True`; a durably-published champion request; a fresh
  approval record carrying two distinct non-requester/non-deployer approvers in the
  `approvers` and `target-approvers` groups; `expires_at` within 24h.
- **Asserts:** (a) the happy path returns a real `AuthorizationGrant` and reaches
  `gate.execute` exactly once; (b) `source.approval_id = None` raises `PermissionError`
  (never a `LookupError` leaking from M06 internals); (c) an expired approval, and an
  approval whose approver group membership is revoked between request and execute,
  each raise `PermissionError` with `gate.execute.assert_not_called()`.
- **Fails today:** (a) raises `ValueError`/`LookupError` from M06 rather than returning
  a grant; (b) raises `LookupError` (wrong type, leaks M06 internals) instead of
  `PermissionError`. This is the single test that would have caught B2 **and** B3.

**Ownership: depends on the option chosen.**
- **Option 1 (recommended) — M10's own paths.** Deleting the dev bypass and requiring
  an `approval_id` is entirely within `optimizer_adapter.py`. **No integrator sanction
  needed.**
- **Option 2 — requires an integrator-sanctioned M06 change.** Adding
  `'optimizer_apply'` to `approvals.py:191` edits M06-owned
  `backend/services/version_control/governance/approvals.py`, which M10 does **not**
  own (module doc §2 lists M06 requester policy as *read only*). That is a reviewed
  cross-module contract change: it must be requested from M06 with a consumer test,
  per `testing-strategy.md` §5 ("New shared fakes/DTOs go through M02; … existing
  mutation callers through M04").

**Recommendation:** take Option 1 and land it inside M10, so this blocker does not
serialize behind an M06 turnaround.

---

### B4 — The live-space benchmark push and quality-enrichment writes are unguarded managed PATCHes; the diff both leaves them unhandled *and* breaks the four-task DAG

**Location** — none of these lines are in the diff; that is the finding.
- `packages/genie-space-optimizer/src/genie_space_optimizer/optimization/preflight.py:2743`
  (`publish_benchmarks_to_genie_space_with_report`)
  → `common/genie_client.py:1641` → `patch_space_config(w, space_id, parsed)`.
- Call site: `packages/genie-space-optimizer/src/genie_space_optimizer/jobs/run_benchmark_qc_and_repair.py:1038`
  — `preflight_push_benchmarks_to_space(w, spark, run_id, space_id, ...)` with the raw
  job `WorkspaceClient` and the **managed live** `space_id`.
- `optimization/space_quality_enrichment.py:751` (`update_space_description`) and
  `:819` (`patch_space_config`), reached from `optimization/unified_loop.py:2833`
  (`run_space_quality_enrichment`).
- Broken call site: `jobs/run_optimize.py:337` — calls
  `run_unified_optimization_loop(w, spark, run_id=..., space_id=..., ...)` with **no**
  `candidate_session` argument.

**Invariant / contract violated**
- F-CHAMPION (`testing-strategy.md` failure matrix): "≤1 optimizer-origin final
  version; **no managed candidate PATCH**."
- Module doc §6 acceptance F-CHAMPION: "no iteration/candidate versions."
- Module doc §8 (explicit pre-condition): "Verify candidates do not currently patch
  the managed live agent; **isolated resource lifecycle is required before enabling
  champion integration**."
- Module doc §6 acceptance: "Existing optimizer telemetry/regression tests stay green
  and remain independently inspectable."
- `AGENTS.md` (package): the four-task DAG contract — "The root bundle and notebook
  installer must keep equivalent task definitions."

**Why it's a bug (source evidence)**
Two distinct problems, same root cause: the isolation guard was added but no isolated
resource was ever created.

*(a) The QC task's live push is a managed PATCH the diff does not handle.*
`common/genie_client.py:2650` (the docstring of `preflight_push_benchmarks_to_space`)
is unambiguous: "push the validated benchmark set into the **LIVE** space at preflight
— BEFORE baseline eval." I traced the chain to a real `patch_space_config` call at
`genie_client.py:1641`. The diff adds `assert_candidate_write` to `patch_space_config`
(diff L398-399), so post-diff this call **raises `PermissionError` at runtime** with a
raw client. But `run_benchmark_qc_and_repair.py` is **task 2 of 4**, ahead of Optimize,
and the diff supplies no `CandidateSession` there and no isolated-resource lifecycle
for benchmark publication. So the job breaks at task 2 — and the package's own
architecture test `tests/unit/test_phase7_job_dag.py::test_repair_task_enforces_corpus_floor_before_publish_and_optimize`
pins that exact call site (`"        _push = preflight_push_benchmarks_to_space("`),
so it cannot simply be deleted.

*(b) The Optimize task is broken outright.* diff L998-999 makes `candidate_session`
mandatory:
```python
if candidate_session is None:
    raise PermissionError("An optimizer-owned CandidateSession is required")
```
but `jobs/run_optimize.py:337` (unmodified, and **not in the diff**) still calls the
loop without it. Post-diff, task 3 raises `PermissionError` on entry. The diff updated
four *tests* to pass `candidate_session` (`tests/unit/test_unified_loop_observed_config.py`,
diff L2166-2197) but not the one **production** caller.

*(c) `IsolationRegistry` has no implementation anywhere.* It is a `Protocol`
(diff L806-809) with `resolve_physical` / `optimizer_owner` / `workspace_id_for`.
I grepped the whole package and `backend/`: no class implements it outside test
fakes, and nothing anywhere constructs a `CandidateSession`. There is also no code
that *creates* an isolated candidate Space — `grep` for
`create_space|clone|candidate_space|duplicate_space` across the package returns
**nothing**. So the "isolated optimizer-owned resource" the whole design rests on does
not exist yet; the guard currently only has the power to *refuse*.

*(d) Enrichment writes are on the same footing.* `space_quality_enrichment.py:751,819`
mutate the live space via the same two functions, reached from `unified_loop.py:2833`
inside a `try/except Exception` that merely logs and continues — so post-diff those
become silently-swallowed `PermissionError`s, degrading enrichment to a no-op without
a visible failure. That is a fail-*quiet*, not a fail-closed.

**Required fix**
Pick one and make it explicit and tested:
1. **Extend the seam:** implement a real `IsolationRegistry` + isolated-resource
   lifecycle (create/own/tear-down a candidate Space per run), construct a
   `CandidateSession` in `run_optimize.py` and for the QC-task benchmark push, and
   route enrichment writes through it. This is the full intent of module doc §8.
2. **Explicitly disable and document (narrower, defensible for this milestone):**
   fail closed and *loudly* on the QC live push and enrichment writes with a recorded
   deployment blocker in `M10_IMPLEMENTATION.md`, and update `run_optimize.py` to
   construct the session (or to refuse with a clear message). Do **not** leave
   enrichment failures swallowed by the `except Exception` at `unified_loop.py:2845`.

Either way `jobs/run_optimize.py:337` must be updated — a mandatory parameter with an
unmodified production caller is a break, not a guard.

**RED test** — `packages/genie-space-optimizer/tests/test_vc_candidate_isolation.py::test_no_job_task_reaches_a_managed_patch_without_a_candidate_session`
- **Seeds:** the four `src/genie_space_optimizer/jobs/*.py` sources plus a raw
  `MagicMock()` workspace client.
- **Asserts:** (a) an architecture/source assertion that every job-task call which
  transitively reaches `patch_space_config` / `update_space_description` originates
  from a `CandidateSession` (this is the same style as the existing, passing
  `test_phase7_job_dag.py` source assertions, so it is idiomatic here); (b) a
  behavioural assertion that
  `preflight_push_benchmarks_to_space(raw_client, ..., managed_space_id, ...)` raises
  `PermissionError` **before** any HTTP call, with `raw_client.mock_calls == []`;
  (c) `run_space_quality_enrichment` with a raw client likewise raises rather than
  being swallowed.
- **Fails today:** (a) fails because `run_benchmark_qc_and_repair.py:1038` and
  `run_optimize.py:337` pass a raw `w`; (b) partially passes by accident (the
  `genie_client` guard fires) but only *after* `fetch_space_config` has already issued
  a live GET at `genie_client.py:1544`, so `mock_calls == []` fails; (c) fails because
  the exception is caught and logged at `unified_loop.py:2845`.

**Ownership: M10's own paths.** Every file involved —
`packages/genie-space-optimizer/src/genie_space_optimizer/{jobs,optimization,common,integration}/**`
and the package `tests/**` — is inside M10's manifest ("Own runtime/source/test/script
files under `packages/genie-space-optimizer/`, except `databricks.yml`"). Notably the
fix does **not** need `databricks.yml` (M08's) since no task definitions change, and
does **not** need `backend/routers/auto_optimize.py` (M04's) since M04 already 503s
the legacy routes. **No integrator sanction needed** — this is squarely M10's to
finish. If option 1 requires new DAB job parameters, *that* would need M08.

---

### B5 — `w is not None` makes the candidate guard opt-out-able, and two `patch_space_config` call sites are untested

**Location**
- `packages/genie-space-optimizer/src/genie_space_optimizer/optimization/applier.py`
  — diff L969-973 (`apply_patch_set`) and diff L981-983 (`rollback`):
```python
if w is not None:                                        # <-- opt-out
    from ...version_control import assert_candidate_write
    assert_candidate_write(w, space_id)
    if apply_mode != "genie_config":
        raise PermissionError("Candidate evaluation cannot mutate shared UC artifacts")
```
- Untested direct call sites (unmodified, not in diff):
  `optimization/applier.py:1970` and `optimization/applier.py:2184` — both
  `patch_space_config(w, space_id, parsed)` inside the prompt-matching auto-config
  path, bypassing `apply_patch_set` entirely.

**Invariant / contract violated**
- Module doc §3: "**isolation cannot be a caller boolean**."
- Module doc §5 task 3: "Route candidate exploration to isolated resources, **never
  temporary live patches**."
- F-CHAMPION: "no managed candidate PATCH."

**Why it's a bug (source evidence)**
`applier.py:4025` types the parameter `w: WorkspaceClient | None` and `rollback`'s
signature at `applier.py:4550` is `w: WorkspaceClient | None` — so `None` is a **live,
supported** call shape, not a theoretical one. With `w=None`:
- `assert_candidate_write` is skipped entirely, and
- the `apply_mode != "genie_config"` UC-artifact check is skipped **too**, because it
  sits *inside* the same `if w is not None:` block (diff L972-973).

So `apply_patch_set(None, space_id, patches, snapshot, apply_mode="both")` proceeds
with **no** isolation proof and **no** UC-mutation refusal. That second point matters:
`applier.py:3972-4008` shows the UC path issues raw
`w.statement_execution.execute_statement("ALTER TABLE ... COMMENT ...")` /
`COMMENT ON TABLE` / arbitrary `update_tvf_sql` DDL against shared Unity Catalog
objects. Gating that refusal on client-non-nullness is the wrong predicate — the
UC-mode check should be unconditional. (`APPLY_MODE` defaults to `"genie_config"` at
`common/config.py:1434`, which limits blast radius in practice, but `apply_mode` is an
explicit caller-supplied parameter, so the default is not a guarantee.)

Meanwhile the diff's own coverage test
`test_all_legacy_mutation_entrypoints_deny_unproven_clients` (diff L1256-1271)
parametrizes exactly four entrypoints — `config`, `description`, `patch_set`,
`rollback` — always with `client = Mock()` and **never** with `None`, and never
touches `applier.py:1970`/`:2184`. Those two call sites do inherit the
`genie_client`-level guard (diff L398-399), which is the saving grace and why this is
B5 rather than higher — but they are unpinned, so a future refactor that adds a
`w=None` fast path or an inlined PATCH silently reopens the hole.

**Required fix**
1. Make the guard unconditional in both `apply_patch_set` and `rollback`: a `None`
   client must raise `PermissionError` (it cannot possibly be an `_EvaluationClient`,
   so `assert_candidate_write(None, space_id)` already does the right thing — just
   drop the `if w is not None:` wrapper).
2. Hoist the `apply_mode != "genie_config"` check **out** of the client-conditional so
   shared-UC mutation is refused regardless of client shape.
3. Extend the deny test to `None` and to the two direct `patch_space_config` call
   sites.

**RED test** — extend `packages/genie-space-optimizer/tests/test_vc_candidate_isolation.py::test_all_legacy_mutation_entrypoints_deny_unproven_clients`
- **Seeds:** add `None` to the client parametrization; add `auto_apply_prompt_matching`
  (the function containing `applier.py:1970`/`:2184`) to the entrypoint
  parametrization; add an `apply_mode="both"` case with `w=None`.
- **Asserts:** every combination raises `PermissionError`, and for non-`None` clients
  `client.mock_calls == []` (no I/O before refusal). For the `apply_mode="both"`,
  `w=None` case specifically, assert the raised message names the shared-UC refusal.
- **Fails today:** the `w=None` cases fall straight through both guards and proceed
  into `apply_patch_set`'s body / `rollback`'s body without raising; the
  `auto_apply_prompt_matching` cases are simply not covered at all.

**Ownership: M10's own paths.** Both `optimization/applier.py` and the package
`tests/**` are inside M10's manifest. **No integrator sanction needed.**

---

## 3. Non-blocking observations

1. **TDD quality is genuinely split by file.** The package tests are real RED-first
   work; the backend adapter test is near-tautological.
   - *Genuine:* `test_every_candidate_mutation_uses_optimizer_owned_evaluation_resource`
     (diff L1220-1237) asserts `client.api_client.do.call_count == 3` **stays** 3 after
     flipping `registry.managed = True` — real negative evidence, not just an
     exception type. `test_legacy_revert_is_disabled_before_any_platform_access` and
     `test_retired_revert_helper_cannot_patch_or_compensate` both assert
     `client.mock_calls == []`, proving refusal *precedes* I/O.
     `test_package_install_exposes_shared_gate_without_importing_backend_app` uses a
     real subprocess + import blocker. All of these fail if the fix is reverted.
   - *Tautological:* `backend/tests/test_vc_optimizer_adapter.py` mocks `MutationGate`,
     `ApprovalService`, `IdentityProvider`, `OperationFacts` **and** the flags provider
     simultaneously, so it can only re-assert the adapter's own arithmetic. **B1, B2 and
     B3 all survive the full 7-test suite green.** Per `testing-strategy.md` §Red→green
     step 4, at least one adapter test must bind to real M06/M08 objects — that single
     test would have caught three of five blockers.

2. **`test_telemetry_memory_fallback_never_authorizes_champion_apply` has ordering the
   module doc doesn't quite ask for.** It asserts `requests.prepare.assert_called_once()`
   — the durable request is persisted *before* the authoritative-facts check fails,
   leaving an idempotency key behind for a run that was never authorized. This is
   permitted by `contracts.md` §2 ("Initial request facts may be persisted before
   reservation; only admission authorizes writes"), so it is not a violation. Worth a
   comment noting the ordering is deliberate.

3. **`ChampionRun.apply` compares `receipt.status` to raw strings across a module
   boundary** (diff L951, L954, L957): `receipt.status in {"applied_partial",
   "applied_unverified", "quarantined"}` and `== "noop"` / `!= "confirmed"`.
   I verified this works today — `OperationStatus` is a `str, Enum`, and
   `OperationStatus.APPLIED_PARTIAL in {'applied_partial', ...}` is `True` with
   `hash(member) == hash(value)`. It would break silently if M04 ever changed
   `OperationStatus` to a plain `Enum`. Since the package deliberately must not import
   `backend.*`, a shared string constant or a documented note would harden this.

4. **17 revert tests deleted — appropriate, with one gap.** All 17 pinned the retired
   in-process PATCH/compensation behaviour
   (`test_revert_compensates_when_description_patch_fails`,
   `test_revert_champion_uses_champion_iteration_config`,
   `test_revert_composes_config_and_benchmark_targets_independently`, …), so deleting
   them is correct — they pinned the bypass. But
   `test_revert_reads_state_with_sp_and_requires_obo_edit_permission` also went, and
   the new guarded path performs **no** `user_can_edit_space` check: it only validates
   that `selection.approval_id` is non-empty (diff L717-719), trusting
   `RestoreJobs.submit`. The M06 rights check for restore now lives entirely behind an
   injected Protocol with no implementation. Defensible (per `contracts.md` §7 the M04
   Job restore is the authority) but the ownership of that check should be recorded
   explicitly in `M10_IMPLEMENTATION.md` so it isn't lost.

5. **`_EvaluationClient` isolation survived adversarial probing.** Executed probes:
   - prefix-extension escape `/api/2.0/genie/spaces/candidate-live-managed` →
     **blocked** (the `path != prefix and not path.startswith(prefix + "/")` check is
     correctly written; a naive `startswith(prefix)` would have leaked).
   - `session.client.statement_execution` → **blocked** by `__getattr__` allowlist
     (`PermissionError: Candidate client does not expose statement_execution`).
   - `client.api_client is client` → `True`, so the guard cannot be stepped around via
     the conventional `.api_client.do` route.
   - `GenieAPI` still works: I confirmed its four eval methods target
     `/api/2.0/genie/spaces/{space_id}/eval-runs[...]`, all under the allowed prefix,
     so read-only benchmark evaluation is unaffected.
   - The `..`/`%`/`?`/`#` rejection is a sensible canonicalization guard.
   *Residual:* `client._client` still exposes the raw client to any in-process caller,
   and `assert_candidate_write` itself reaches through `client._session`. That is a
   convention, not a boundary — acceptable for a same-process seam, worth a comment.

6. **`w` also flows to non-Genie services that the guard cannot cover, and this is
   fine but should be stated.** From inside `run_unified_optimization_loop`, `w` reaches
   `run_bounded_profile` (`unified_loop.py:2741`, inside the nested closure
   `_adapt_wide_schema_for_failures` at `:2645`), which issues
   `w.statement_execution.execute_statement` for column profiling
   (`wide_schema_profile.py:276`). The diff rebinds `w = candidate_session.client`
   (diff L1004), whose `__getattr__` allowlist exposes only `genie` and `config` — so
   profiling would raise `PermissionError` the moment a `pending` column exists **and**
   `GENIE_SPACE_OPTIMIZER_WAREHOUSE_ID` is set. This is arguably *correct* (candidate
   evaluation shouldn't profile shared UC), but it is an unremarked behavioural change
   to a working feature and is not covered by any test in the diff. Same shape for
   `leakage.preflight_embedding_endpoint` / the `w.serving_endpoints.query` path at
   `leakage.py:193` — though I confirmed `unified_loop.py:40-44` imports only
   `BenchmarkCorpus`, `canonicalize_sql`, `is_benchmark_leak` from `leakage`, none of
   which take a client, so the embedding path is **not** reachable from the loop and is
   not affected. The profiling path is.

7. **`OptimizerRuntime.contract_version = "VC/1.0"` is an un-annotated class attribute
   inside a `@dataclass(frozen=True)`** (diff L907), so it is correctly *not* a field —
   which is why the entry-point test's 3-positional-arg construction
   `runtime_type(adapter, restore_jobs, isolation_registry)` works. Intentional and
   correct, but one stray `: str` annotation would break the entry point and the test
   would then fail confusingly inside the subprocess. Worth a `# not a field` comment.

8. **M08 composition does not yet load the entry point.** `grep` for
   `entry_points|genie_space_optimizer.vc|OptimizerRuntime|optimizer_adapter` across
   `backend/` (excluding tests) returns **nothing**. The packaging invariant (task 13)
   is satisfied on the *package* side — the entry point exists, is declared in
   `pyproject.toml` (diff L375-376), and loads without importing `backend`/`fastapi` —
   but the M08 side of the seam is unwired, and nothing constructs
   `OptimizerChampionAdapter` in composition. Correctly **outside** M10's ownership
   (module doc §4 assigns target-local composition to M08); flagged only so the seam
   isn't mistaken for end-to-end done.

9. **`discard.py` change is correct and honestly messaged.** The `rollback(...)` call
   and its `config_snapshot` precondition are removed (diff L501-523), the status
   UPDATE is retained, and the user-facing message is truthfully rewritten to
   "Optimization discarded; managed state unchanged. Use a governed restore Job for
   historical state." `test_discard_only_updates_telemetry_and_never_rolls_back_managed_state`
   asserts `rollback.assert_not_called()` **and** `write.assert_called_once()` — both
   the negative and the retained positive. Good.

10. **`addopts` placement is consistent, if arguably M08-adjacent.** Adding
    `addopts = "-m 'not integration'"` + `markers = [...]` to
    `packages/genie-space-optimizer/pyproject.toml` (diff L385-386) mirrors the root
    `pyproject.toml:44-45` stanza exactly. `testing-strategy.md` says "Register an
    `integration` marker using **M08-owned test configuration**," which could be read
    as M08's call — but the package `pyproject.toml` is explicitly M10-owned
    (only `databricks.yml` is carved out), and the root stanza already established the
    pattern. I read this as consistent rather than an overlap. Not blocking.

---

## 4. Owned-paths compliance — **PASS**

M10's manifest: `packages/genie-space-optimizer/**` **except** its `databricks.yml`;
`backend/services/version_control/optimizer_adapter.py`;
`backend/tests/test_vc_optimizer_adapter.py`; package tests
`test_vc_champion_apply.py` / `test_vc_candidate_isolation.py` / `test_vc_revert_guard.py`.

All 19 files touched by the diff, classified:

| # | File | In manifest? |
|---|---|---|
| 1 | `backend/services/version_control/optimizer_adapter.py` | ✅ named explicitly |
| 2 | `backend/tests/test_vc_optimizer_adapter.py` | ✅ named explicitly |
| 3 | `packages/genie-space-optimizer/M10_IMPLEMENTATION.md` | ✅ under package |
| 4 | `packages/genie-space-optimizer/pyproject.toml` | ✅ under package (not `databricks.yml`) |
| 5 | `…/src/genie_space_optimizer/common/genie_client.py` | ✅ under package |
| 6 | `…/src/genie_space_optimizer/integration/apply.py` | ✅ under package |
| 7 | `…/src/genie_space_optimizer/integration/discard.py` | ✅ under package |
| 8 | `…/src/genie_space_optimizer/integration/revert.py` | ✅ under package (the *real* revert path, as §2 required be discovered) |
| 9 | `…/src/genie_space_optimizer/integration/version_control.py` | ✅ new, under package |
| 10 | `…/src/genie_space_optimizer/optimization/applier.py` | ✅ under package |
| 11 | `…/src/genie_space_optimizer/optimization/unified_loop.py` | ✅ under package |
| 12 | `…/tests/integration/test_vc_optimizer_platform.py` | ✅ new, under package |
| 13 | `…/tests/test_vc_candidate_isolation.py` | ✅ named explicitly |
| 14 | `…/tests/test_vc_champion_apply.py` | ✅ named explicitly |
| 15 | `…/tests/test_vc_revert_guard.py` | ✅ named explicitly |
| 16 | `…/tests/unit/test_applier_update_space_description.py` | ✅ under package |
| 17 | `…/tests/unit/test_patch_space_config_array_coercion.py` | ✅ under package |
| 18 | `…/tests/unit/test_revert_champion_config.py` | ✅ under package |
| 19 | `…/tests/unit/test_unified_loop_observed_config.py` | ✅ under package |

**Specifically verified clean:**
- `packages/genie-space-optimizer/databricks.yml` — **NOT touched.** `grep -n
  "databricks.yml" .polly/m10.diff` returns nothing, and the file appears nowhere in
  the diff's `diff --git` list. M08's ownership respected.
- `backend/routers/auto_optimize.py` and its tests (M04-owned, read-only for M10) —
  **NOT touched.** Correct: M04 has already stubbed `/runs/{run_id}/apply`,
  `/discard` and `/revert` to `503 vc_writes_disabled`
  (`auto_optimize.py:1969-2008`), so no live legacy HTTP bypass survives and M10 had
  no need to edit a consumer.
- Root `pyproject.toml`, `uv.lock`, `requirements.txt`, root `databricks.yml`
  (all M08) — **NOT touched.**
- `backend/services/version_control/contracts.py` (M02),
  `mutation_gate.py` (M04), `governance/approvals.py` (M06),
  `platform/feature_flags.py` (M08) — **NOT touched.** This is correct discipline,
  and it is also *why* B1/B2/B3 exist: the adapter guessed at those contracts instead
  of testing against them. The right remedy is a contract-bound test, not an edit to
  those files (see per-blocker ownership notes in §2).

**No ownership violations found.**

---

## 5. Integration-test honesty — **PASS**

The six real-platform probes in
`packages/genie-space-optimizer/tests/integration/test_vc_optimizer_platform.py` are
**honestly marked as deployment blockers and are not claimed as passing.**

1. **Marked.** `pytestmark = pytest.mark.integration` at module level (diff L1028) —
   module scope, so it cannot be partially applied. This matches the existing
   repository convention (`backend/tests/integration/test_vc_delta_cas.py:49`).
2. **Deselected offline by default.** `addopts = "-m 'not integration'"` added to the
   package `pyproject.toml` (diff L385-386), with the marker registered alongside.
   Mirrors root `pyproject.toml:44-45` exactly.
3. **Collectable only with explicit selection.** `M10_IMPLEMENTATION.md` documents both
   commands: `uv run --no-sync pytest --ignore=tests/integration -q` for offline, and
   `pytest -m integration tests/integration/test_vc_optimizer_platform.py` for
   explicit selection.
4. **Missing config is a FAILURE, not a skip.** diff L1096-1098:
   ```python
   if not location:
       pytest.fail("DEPLOYMENT BLOCKER: VC_M10_INTEGRATION_CONFIG and M04/M06/M08 composition required")
   ```
   `pytest.fail`, **not** `pytest.skip`. This satisfies `testing-strategy.md`
   §Definition of done: "unavailable integrations are explicit blockers to affected
   feature enablement, **not silent test skips**," and §Red→green step 4: "Never
   describe skipped integration tests as passed."
5. **Written down as unvalidated, in the implementer's own words.**
   `M10_IMPLEMENTATION.md` (diff L336-341): "`tests/integration/test_vc_optimizer_platform.py`
   is **not platform-validated**. Its six Jobs/Genie/Delta probes remain a deployment
   blocker until M04/M06/M08 target-local composition and disposable fixtures are
   deployed and exercised. Offline success and collection-only checks do not satisfy
   this blocker."
6. **Count matches:** six probes declared, six present —
   `test_live_candidates_and_abandon_do_not_mutate_managed_binding`,
   `test_live_champion_retry_preserves_one_authoritative_optimizer_version`,
   `test_live_external_preimage_is_preserved_separately`,
   `test_live_noop_champion_has_durable_receipt_without_version`,
   `test_live_ambiguous_apply_cannot_replay_or_compensate`,
   `test_live_historical_restore_uses_job_and_restored_from_link`.
7. **Fixture safety is asserted, not assumed:** `disposable_nonproduction is True`,
   `profile.upper() != "DEFAULT"` (no auto-profile-selection — matches the
   session-level Databricks rule), host/workspace-id cross-checks against SCIM `Me`,
   and required job tags `vc_test_environment=disposable` / `vc_module=m10` with a
   pinned `run_as` service principal (diff L1046-1083). SQL identifiers and literals
   go through `_identifier` / `_literal` regex validators. Good.

**One caveat, non-blocking:** the offline pass claim in `M10_IMPLEMENTATION.md`
("Verified September 7, 2026: **893 package offline tests passed**, **7 backend
adapter tests passed**") is scoped and plausible, but I could **not** re-execute it —
the sandbox denied copying/cloning the repo to a scratch worktree, and the diff is
unapplied so there is nothing to run in place. Given B1–B3 all survive that green
suite, the number should be read as "the mocks agree with themselves," not as evidence
of composition correctness. Recommend re-running after the B1/B2/B3 contract-bound
tests are added, and noting the new counts.

---

## 6. Blocker ownership summary

| ID | Summary | Fix location | Cross-module sanction needed? |
|---|---|---|---|
| **B1** | Invented flag `vc_optimizer_contract_ready` → `KeyError` when writes enabled | `optimizer_adapter.py:82` — **drop the invented flag name** | **No.** M10-own-paths. Do *not* solve by asking M08 to add the field — M08 alone owns `FeatureFlags`, and the two existing switches already suffice. |
| **B2** | `request_digest` uses M10-private domain → real M06 `authorize()` always rejects | `optimizer_adapter.py:103` — use M06/M02 `request_digest` | **No.** M10-own-paths; importing M06's `request_digest` is legal (M06 is a declared read-only dependency). *Optionally* ask M02 to re-export it (1-line additive, integrator-sanctioned) as hygiene — not required. |
| **B3** | Both M06 grant branches dead for `operation_type='optimizer_apply'` | **Option 1 (recommended):** `optimizer_adapter.py:99-102` — require a real `approval_id`, delete dev bypass. **Option 2:** `approvals.py:191` — add `'optimizer_apply'` to rights-eligible types | **Option 1: No** (M10-own-paths). **Option 2: YES** — edits M06-owned `governance/approvals.py`, which module doc §2 lists as read-only for M10; needs a reviewed M06 contract change + consumer test. **Take Option 1** so this doesn't serialize behind M06. |
| **B4** | QC-task live benchmark push + enrichment unguarded; `run_optimize.py:337` breaks on mandatory `candidate_session`; no isolated-resource lifecycle exists | `packages/genie-space-optimizer/src/.../{jobs,optimization,common}/**` + package tests | **No.** All inside M10's manifest. Does not need `databricks.yml` (no task-definition change) nor `auto_optimize.py` (M04 already 503s). *If* the chosen design adds new DAB job parameters, **that** part would need M08. |
| **B5** | `if w is not None:` makes the isolation guard and the shared-UC refusal opt-out-able; `applier.py:1970/:2184` untested | `optimization/applier.py` (diff L969-973, L981-983) + package tests | **No.** M10-own-paths. |

**Bottom line:** **four of five blockers (B1, B2, B4, B5) are fixable entirely inside
M10's own owned paths, and B3 is too if Option 1 is taken.** No integrator-sanctioned
cross-module change is *required* to clear this review. B1 and B2 are single-line
changes whose absence is itself the evidence of the missing provider-contract test —
that one test (`test_champion_apply_authorizes_through_real_approval_service`, wired to
the real `ApprovalService` and real `FeatureFlags`) would have caught three of the five.

## 7. Path to APPROVE

1. Fix **B1** (drop invented flag) and **B2** (correct digest domain) — two lines.
2. Fix **B3** via Option 1 (require real `approval_id`; delete the dev bypass).
3. Add the **one** contract-bound adapter test wiring real `FeatureFlags` +
   real `ApprovalService` + the M02 fake fact store. This is the structural remedy:
   without it, the next mock-only adapter change reintroduces this class of defect.
4. Close **B4** — at minimum update `jobs/run_optimize.py` to construct the
   `CandidateSession`, and either guard or explicitly-and-loudly disable the QC-task
   live benchmark push and the enrichment writes, with the decision recorded in
   `M10_IMPLEMENTATION.md`.
5. Close **B5** — make the guard and the shared-UC refusal unconditional; extend the
   deny test to `w=None` and to `applier.py:1970`/`:2184`.
6. Re-run the package suite + `backend/tests/test_vc_optimizer_adapter.py` and update
   the recorded counts in `M10_IMPLEMENTATION.md`. Leave the six platform probes
   marked as blockers — that part is already right.

---

*Reviewed by: independent cross-vendor reviewer (Claude). Implementer: GPT/Astra.
Review is diff-text + `HEAD`-source based, with executable probes for B1/B2/B3 run in
a scratch directory. No files in the implementer's worktree were modified.*
