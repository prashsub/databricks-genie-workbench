# M09 Frontend / Workbench Version-Control UI — independent cross-vendor review, as a fix specification

**Reviewer:** independent vendor (Claude), different from the implementer (GPT/Astra).
**Reviewed artifact:** `.polly/m09.diff` (1347 lines) as diff *text*. The implementer's worktree was
neither opened nor executed.
**Authority:** `docs/design/version_control/implementation_plan/module-09-workbench-frontend.md`,
`contracts.md` (VC/1.0), `testing-strategy.md`.

## Verdict: BLOCK

Eight blocking findings (B1–B8) below. Each is self-contained: exact location in the diff, the
invariant violated, the minimal fix, and the named RED test to write *first*. Two of the eight
(B3, B7) require **rewriting an existing test that currently cannot fail or actively asserts the
defect** — for those, the first step is making the existing test red, not adding a new one.

## Ownership: clean — no scope violation

Every hunk in the diff lands under `frontend/src/**`:

| Path | Status |
|---|---|
| `frontend/src/App.tsx` | modified (mount only) |
| `frontend/src/components/version-control/*` | new (16 files incl. tests) |
| `frontend/src/hooks/use-version-control.ts` / `.test.ts` | new |
| `frontend/src/lib/version-control-api.ts` / `.test.ts` | new |
| `frontend/src/types/version-control.ts` | new |
| `frontend/src/pages/VersionControlWorkbench.tsx` | new |

No backend file, no root file, no lockfile, no other module's directory. Module plan §2
("own **all touched `frontend/**` paths**") is satisfied and nothing outside it is touched.
All 16 named RED tests from module plan §5 exist under the exact required filenames, plus two
extras (18 `it(...)` cases total).

## What the diff got RIGHT — do not regress these while fixing B1–B8

These four properties are correct, load-bearing, and easy to break accidentally during the fixes.
Any patch that weakens one of them should be rejected even if it closes a blocker.

1. **Transport injection is genuine.** `lib/version-control-api.ts:38-39` —
   `VersionControlApi` declares `private transport: typeof fetch` and
   `constructor(transport: typeof fetch) { this.transport = transport }`. There is **no default
   parameter**, so the class cannot reach the network unless a caller hands it a real `fetch`.
   Every request funnels through `this.transport` in exactly one place
   (`lib/version-control-api.ts:42`). Preserve the single choke point.
2. **The no-network claim is *proven*, not asserted.**
   `components/version-control/version-control-tab.test.tsx:57-82`
   (`fake_api_supports_approval_restore_promotion_and_receipt_without_network`) stubs the global
   with `vi.stubGlobal('fetch', network)` where `network` throws, drives the full
   overview→observe→approve→vote→restore→release→validate→promote→receipt sequence against
   `createDemoTransport()`, and closes with `expect(network).not.toHaveBeenCalled()`
   (`:81`), unstubbing in a `finally`. This is the strongest test in the submission. Keep it, and
   extend it rather than replacing it.
3. **Overview is a single paged projection — no per-row live call.**
   `components/version-control/overview-badge.test.tsx:21-30`
   (`overview_does_not_fetch_live_genie_per_row`) builds a 25-item page, asserts
   `expect(transport).toHaveBeenCalledTimes(1)` (`:26`), asserts the exact cursor URL
   `/api/version-control/overview?limit=25&cursor=page%2F2` (`:28`), and asserts
   `transport.mock.calls.every(([url]) => String(url).includes('/overview?'))` (`:29`) — a real
   negative-space claim that no other endpoint was touched. This satisfies module plan §5 task 3
   and contracts §7 `GET /overview` "no live GET per row". Preserve the call-count and
   every-URL assertions.
4. **`In sync` derives from approved/deployed policy equality, never `live == observed`.**
   `components/version-control/overview-badge.tsx:5-7`:
   ```ts
   if (status.drift === 'clean') {
     label = !status.heads.approved || !status.heads.deployed ? 'Unknown'
           : status.heads.approved === status.heads.deployed ? 'In sync' : 'Policy mismatch'
   }
   ```
   `status.heads.observed` is absent from this decision entirely, and the `labels` map at `:3`
   deliberately maps raw `clean → 'Policy mismatch'` so an unexamined `clean` can never surface as
   `In sync`. Missing approved *or* deployed yields `Unknown`, satisfying module plan §8
   ("Do not label missing approved/deployed history 'In sync'") and contracts §8 ("Badge `In sync`
   derives from approved/deployed policy equality, never simply live==observed").
   `overview-badge.test.tsx:9-11` pins all three branches. **This is the single most important
   invariant in M09 — B1's fix touches this same function, so re-run these three assertions after
   changing it.**

Also verified correct and worth keeping: short IDs carry the full value in `title` and are never
presented as ordering (`overview-badge.tsx:12`, see non-blocking item 11); generation/abort
handling in the store genuinely defeats a late response
(`hooks/use-version-control.ts:19-28` + `hooks/use-version-control.test.ts:6-23`); the demo
transport enforces `Idempotency-Key` on every POST (`components/version-control/demo-api.ts:38`)
and refuses any non-VC path with no network fallback (`demo-api.ts:36`, `:122`).

---

# Blocking findings

Fix order below is deliberate: B1→B2 unblock the two acceptance criteria that are outright
unmet; B3→B5 are correctness/authority defects in shipped code; B6→B8 close the test-depth
holes that let B1–B5 through in the first place. B6 should be done before B8 because the M02
fixtures it introduces are the input data B8's three-status test needs.

---

### B1 — `unresolved_operation_id` and `reasons` are never rendered; quarantine overwrites conflicted; stale-clean collapses into unknown

**(a) Location**

- `components/version-control/overview-badge.tsx:2-15` — the whole `OverviewBadge` body.
  Specifically the label pipeline at `:4-9` and the render at `:10-14`.
- `components/version-control/version-control-tab.tsx:17-29` — `VersionControlView`.
- `components/version-control/reconcile.tsx:36-49` — `ReconcilePanel.action`, which reports the
  outcome as a bare string at `:44` and never mounts `OperationStatusView`.
- Demo data proving the collapse is reachable: `components/version-control/demo-api.ts:147`
  (`demo-partial`: `unresolved_operation_id: 'partial-operation'` with `drift` left at the
  fixture's `'diverged'`).

**(b) Defect and invariant violated**

`BindingStatus.unresolved_operation_id` and `BindingStatus.reasons` are both declared in
`types/version-control.ts:8-9` and both populated by fixtures, but **neither is rendered anywhere
in the diff**. Three distinct collapses follow:

1. **`applied_partial` is invisible.** `applied_partial` is *not* a member of `DriftState`
   (`types/version-control.ts:1` — the union is `clean | external_ahead | desired_ahead |
   diverged | unknown | unreachable | applied_unverified | conflicted`). It is only an
   `OperationStatus`. Therefore `unresolved_operation_id` is the **sole** channel by which the
   overview can signal "a partial write is outstanding on this binding" — and it is dropped. The
   demo's own `demo-partial` row renders a badge byte-identical to a plain diverged binding.
2. **Quarantine overwrites conflicted.** `overview-badge.tsx:8`,
   `if (status.quarantined) label = 'Quarantined'`, is an unconditional *override*. A binding that
   is simultaneously `quarantined: true` and `drift: 'conflicted'` loses the conflict entirely —
   the operator cannot tell "quarantined, external state preserved" from "quarantined, no
   conflict".
3. **Stale-clean is indistinguishable from unknown.** `overview-badge.tsx:9`,
   `if (status.stale && label === 'In sync') label = 'Unknown'`, is safe in direction (it refuses
   to claim sync on stale data — good) but it produces a badge identical to a genuine
   `drift: 'unknown'`. "We cannot see right now" and "the canonicalizer cannot compare these" are
   different operator actions.

Additionally, dropping `reasons` means the drift *explanation* never reaches the UI at all.

Violates:
- **Module plan §6 AC "FM-OUTAGE/FM-RECOVERY: stale/unreachable/**partial**/quarantine states
  distinguishable; no blind retries."** Partial is not distinguishable. Directly unmet.
- **`testing-strategy.md` FM-RECOVERY, frontend column: "Quarantine/partial/unverified
  distinguishable, no retry-PATCH control."** Partial is not distinguishable.
- **Module plan §5 task 10 GREEN: "Read-only verification/status view with operator evidence
  explanation."** Reconcile outcomes get a string, not the evidence view.
- **`testing-strategy.md` FM-DUAL-AUTHORITY, frontend column: "Drift reason identifies dual
  authority; no inferred actor."** `reasons` is never rendered, so no drift reason can ever
  identify anything. Unmet by construction.

**(c) Minimal fix**

1. In `OverviewBadge`, make quarantine and staleness **additive markers**, not label overrides.
   Keep the `drift === 'clean'` policy branch exactly as-is (see "What the diff got RIGHT" #4);
   add alongside the label: a quarantine marker, a stale marker that is textually distinct from
   the `unknown` drift label, an `unresolved_operation_id` marker, and the `reasons` list.
2. Render `unresolved_operation_id` as a drill-through control that loads
   `GET /operations/{id}` (the API method already exists: `lib/version-control-api.ts:69`) into
   `OperationStatusView`.
3. Render `reasons` in both `OverviewBadge` and `VersionControlView`.
4. Route `ReconcilePanel`'s result through `OperationStatusView` — the component already exists
   and is already exported (`components/version-control/reconcile.tsx:8`); `RestorePanel` already
   uses it (`restore.tsx:51`). This is a wiring change, not new UI.

**(d) RED test to write first**

`components/version-control/overview-badge.test.tsx`:

```
badge_distinguishes_partial_unresolved_quarantined_conflicted_and_stale_clean
```

Assert a **pairwise-distinct** matrix — that is the assertion shape that actually forbids
collapse, rather than checking for the presence of one string:

- `{drift:'diverged', unresolved_operation_id:'op-1'}` renders **differently** from
  `{drift:'diverged', unresolved_operation_id:null}`, and the former contains `op-1`.
- `{quarantined:true, drift:'conflicted'}` retains **both** signals (contains a quarantine marker
  **and** a conflicted marker).
- `{drift:'clean', stale:true, heads:{approved:'p', deployed:'p', observed:'x'}}` renders
  **differently** from `{drift:'unknown'}`.
- `reasons: ['dual_authority_detected']` appears in the output.
- Regression guard, re-asserting the correct existing behaviour: the same three `clean`-branch
  cases from `overview-badge.test.tsx:9-11` still hold.

A clean way to make the distinctness claim un-fakeable: render each matrix entry, collect the
markup into an array, and assert `new Set(markups).size === markups.length`.

`components/version-control/reconcile.test.tsx`:

```
reconcile_result_renders_operation_evidence_not_a_bare_message
```

Drive `ReconcilePanel` to a completed `acknowledge` whose polled operation returns
`status:'applied_partial'` with `checkpoints`/`audit` populated, and assert the checkpoint and
audit strings appear in the markup.

---

### B2 — `adopt` never POSTs `reconcile`; the flow dead-ends at "request approval"

**(a) Location**

- `components/version-control/reconcile.tsx:21` — the unconditional early return:
  ```ts
  if (action === 'adopt') return api.requestApproval(inputs, key)
  ```
- Button that reaches it: `components/version-control/reconcile.tsx:52` (label
  `'Request adoption approval'`).
- Unreachable server branch that proves the gap: `components/version-control/demo-api.ts:203-208`
  — the demo transport fully implements `action: 'adopt'` on
  `POST /bindings/{id}/reconcile`, including the fresh-approval check at `:207`. No code path in
  the diff can reach it.

**(b) Defect and invariant violated**

`reconcileAction` returns *unconditionally* for `adopt`. `POST /bindings/{id}/reconcile` with
`action: 'adopt'` is therefore **never issued from anywhere in the module** — not even when a
fresh, valid, correctly-base-bound approval is already in hand. The button re-requests a brand
new approval on every click, so the operator can accumulate approvals forever and never adopt.

Note the asymmetry that makes this unambiguous rather than a design choice: `reapply` and
`acknowledge` both fall through to the real POST at `reconcile.tsx:26`. Only `adopt` short-circuits.

Violates:
- **Module plan §6 AC "F-FRESH-APPROVAL/F-PROMOTION: users can approve/reconcile/promote
  **entirely inside Workbench**."** Adoption cannot be completed inside Workbench — or anywhere.
- **Module plan §5 task 9 GREEN: "Adopt/compare/reapply/acknowledge flows reflecting server
  policy."** The adopt flow is absent, and the server policy at `demo-api.ts:207` is unreachable.
- **`contracts.md` §6: "Adoption files a new request."** Filing the request is step one of two;
  the contract describes what makes an adoption approval *fresh*, not a claim that filing is the
  entire flow. Contracts §7 lists `action=adopt` as a first-class verb on
  `POST /bindings/{binding_id}/reconcile`.

**(c) Minimal fix**

Make adopt two-phase inside `reconcileAction`:

- If no approval is bound, or the bound approval is not `valid`, or its
  `expected_base_fingerprints` do not match the reviewed base → `requestApproval` (current
  behaviour, now conditional).
- Otherwise → fall through to the existing `api.reconcile(...)` call at `reconcile.tsx:26` with
  `action:'adopt'`, carrying `binding_revision`, `expected_base` and `approval_id`.

Then give the button a two-state label so the operator can see which phase they are in
("Request adoption approval" vs "Adopt captured state with approval").

**(d) RED test to write first**

`components/version-control/reconcile.test.tsx`:

```
adopt_with_fresh_bound_approval_posts_reconcile_action_adopt
```

Asserts, with a valid approval whose `expected_base_fingerprints` equal the reviewed base:
- the POST URL is exactly `/api/version-control/bindings/demo-binding/reconcile`;
- the body contains `action:'adopt'` **and** `binding_revision`, `expected_base`, `approval_id`;
- and — the half that keeps the fix honest — with a *stale* approval (expired `expires_at`, or
  `expected_base_fingerprints` differing from the reviewed base), the only call made is to
  `/api/version-control/approvals` and **no** `/reconcile` POST is issued.

The second half must be present. Without it, an implementer can satisfy the test by always
POSTing adopt, which would trade this blocker for an approval-freshness violation.

---

### B3 — Production code posts test-fixture `ApprovalInputs`, including a client-asserted `requester_id` and a fabricated `preflight_evidence_digest`; and the existing test asserts the defect

**(a) Location**

- `components/version-control/version-control-tab.tsx:14` — a **non-test module importing the
  fixture module**: `import { approvalInputsFixture } from './fixtures'`.
- `components/version-control/version-control-tab.tsx:38` — the spread that becomes the request
  body:
  ```ts
  const inputs = { ...approvalInputsFixture, target_binding: bindingId,
    source_version_id: base?.version_id ?? '',
    expected_base_fingerprints: base?.fingerprints ?? approvalInputsFixture.expected_base_fingerprints }
  ```
- `components/version-control/promotion.tsx:112` — re-spreads the same object for the promotion
  approval, overriding only `operation_type`, `source_version_id`, `target_binding`,
  `mapping_digest`.
- The fabricated values: `components/version-control/fixtures.ts:15-22` (`approvalInputsFixture`),
  notably `operation_id: 'demo-operation'`, `raw_source_digest: 'raw-3'`,
  `rendered_target_digest: 'rendered-3'`, `validation_policy_digest: 'validation-1'`,
  `benchmark_policy_digest: 'benchmark-policy-1'`,
  **`preflight_evidence_digest: 'preflight-1'`**, **`requester_id: 'demo-requester'`**,
  `recovery_policy: 'quarantine'`, `expires_at: '2026-09-07T23:00:00Z'`.
- **The test that locks the defect in:** `components/version-control/reconcile.test.tsx:14`
  ```ts
  expect(JSON.parse(String(transport.mock.calls[0][1]?.body))).toEqual(approvalInputsFixture)
  ```

**(b) Defect and invariant violated**

The shipped `VersionControlTab` constructs the `ApprovalInputs` it POSTs to `/approvals` by
spreading a **test fixture** and overriding three fields. Everything else on the wire is
browser-invented. Two of the invented fields are authority-bearing:

- **`requester_id: 'demo-requester'`** — a client-asserted identity in an approval request body.
- **`preflight_evidence_digest: 'preflight-1'`** — a constant. In `PromotionPanel` the operator
  runs `validate()` (`promotion.tsx:34-41`, which polls a real preflight operation) and then
  requests an approval whose preflight digest has **no causal relationship whatsoever** to the
  preflight evidence that validate just produced. The approval therefore cannot bind the
  preflight, which is the entire point of the gate.

Violates:
- **Module plan §3: "Browser sends idempotency key, reviewed base and binding revision; it
  **never sends an approver identity as proof**, service credentials, or direct Genie PATCH."**
- **`contracts.md` §6: "Approval digest additionally binds **server-derived** approver identities
  and approval timestamps."** A client-supplied `requester_id` is the wrong side of that line.
- **`contracts.md` §7: "Never accept credentials or an approver identity as authorization."**
- **Module plan §5 task 12: "Version→mapping→**validate**→approve→promote flow"** — the approve
  step must bind the validate step's evidence. A constant digest breaks the chain.
- **Module plan §8: "Client auth checks are convenience only."** Fabricated policy digests are
  not convenience; they are the substance of the approval.

**Independently: `reconcile.test.tsx:14` asserts the defect.** It requires that the client post
`approvalInputsFixture` **verbatim**. Any correct fix — deriving the digests from server
projections, dropping `requester_id` — makes this assertion fail, so a conforming implementation
would be scored as a regression. This test must be rewritten as part of the fix, not preserved.

**(c) Minimal fix**

1. **Delete `./fixtures` from every non-test import.** `fixtures.ts` must be reachable only from
   `*.test.ts(x)` and from `demo-api.ts` (which is itself a fake). This single constraint is
   mechanically checkable and is the root cause.
2. **Omit server-derived fields from the client body entirely** — `requester_id` and
   `operation_id` come from the authenticated server side. Make them optional in
   `ApprovalInputs` for *response* parsing, and use a distinct narrower request type for what the
   browser sends.
3. **Derive the rest from server-supplied projections:** reviewed base fingerprints from the
   observed `VersionSummary` (already available as `base` at `version-control-tab.tsx:37`);
   `preflight_evidence_digest` from the `validate` operation's returned evidence — which means
   `Operation` needs an evidence-digest field, and `createPromotionFlow.validate`
   (`promotion.tsx:31-41`) must return it and thread it into the approval inputs; policy digests
   from `BindingStatus`/the release response rather than from a literal.
4. If a value is genuinely not yet available from any backend seam, the correct interim is an
   **explicit absent/unknown state that blocks the request**, not a plausible-looking constant.

**(d) RED test to write first**

First, **rewrite the defect-asserting assertion.** `reconcile.test.tsx:14` currently pins
`toEqual(approvalInputsFixture)`. Replace it with an assertion that the posted body is
*derived*: that `expected_base_fingerprints` equals the observed version's fingerprints, and that
the body has **no** `requester_id` key.

Then add, in `components/version-control/approvals.test.tsx`:

```
approval_request_body_carries_server_derived_preflight_and_no_client_requester_identity
```

asserting the POSTed body: `expect(body).not.toHaveProperty('requester_id')`,
`expect(body).not.toHaveProperty('operation_id')`, and that no value in the body equals any
literal from `fixtures.ts` (`'preflight-1'`, `'validation-1'`, `'raw-3'`, `'rendered-3'`).

And in `components/version-control/promotion.test.tsx`:

```
promotion_approval_binds_the_preflight_evidence_digest_returned_by_validate
```

Have the faked `validate` operation return a distinctive evidence digest, then assert that exact
value appears as `preflight_evidence_digest` in the subsequent `/approvals` POST body — and that
changing the selection (which already resets `preflight` via
`createPromotionFlow.configure`, `promotion.tsx:29`) invalidates it.

Finally, a structural guard worth its own cheap test in
`components/version-control/version-control-tab.test.tsx`:

```
production_modules_do_not_import_test_fixtures
```

Read the non-test sources under `components/version-control/` and assert none except `demo-api.ts`
contains `from './fixtures'`. This prevents silent reintroduction.

---

### B4 — `createMutationIntent` is a single-slot memo: an A→B→A interleave re-mints the key and re-sends the command

**(a) Location**

- `lib/version-control-api.ts:21-29` — `createMutationIntent`:
  ```ts
  export function createMutationIntent() {
    let previous = ''
    let key = ''
    return { key: (body: unknown) => {
      const serialized = JSON.stringify(body)
      if (!key || serialized !== previous) { previous = serialized; key = crypto.randomUUID() }
      return key
    } }
  }
  ```
- The dedupe cache that cannot compensate: `lib/version-control-api.ts:47-58` — `post` keys
  `this.commands` by `key`, not by intent.
- Reachable shared-intent call sites: `components/version-control/reconcile.tsx:39`
  (one `intent` shared across adopt / reapply / acknowledge, used at `:42`) and
  `components/version-control/promotion.tsx:27` (one `intent` shared across validate at `:38`
  and promote at `:46`).
- The test that misses it: `lib/version-control-api.test.ts:25-42`
  (`double_click_or_network_loss_does_not_generate_new_mutation_key`) exercises only A→A→A and
  A→B. The A→B→A interleave is never tried.

**(b) Defect and invariant violated**

`createMutationIntent` remembers exactly **one** prior intent. Trace it:

1. `key(A)` → `previous = A`, mints `keyA`. Returns `keyA`.
2. `key(B)` → `B !== A`, `previous = B`, mints `keyB`. Returns `keyB`.
3. `key(A)` again → `A !== previous(B)` → **mints a brand-new `keyA'`**.

Because `VersionControlApi.post` dedupes on the *key* (`lib/version-control-api.ts:49`,
`this.commands.get(key)`), `keyA'` misses the cache. A **second live POST of the byte-identical
command goes out under a fresh `Idempotency-Key`.** The server sees a new idempotency key with the
same request digest — i.e. a new intent — and is entitled to mutate a second time. The client-side
idempotency guarantee is silently void.

This is not a theoretical interleave. It is the normal interaction pattern at both shared-intent
call sites: in `ReconcilePanel` the operator clicks adopt, then reapply, then adopt again; in
`PromotionPanel` validate and promote alternate by design.

Note the irony that makes this a genuine TDD miss rather than an oversight: `post`'s
intent-mismatch branch (`lib/version-control-api.ts:50-51`) is *correct* and well-tested
(`version-control-api.test.ts:39`, `rejects.toThrow('different intent')`). The bug is that a
recurring intent never *reaches* that branch, because it arrives under a key the cache has never
seen.

Violates:
- **Module plan §5 task 14 GREEN: "**Stable per-intent** idempotency and operation polling;
  changed intent gets new key."** The key is stable per *most-recent* intent, not per intent.
- **`testing-strategy.md` failure matrix F-IDEMPOTENCY: "Same key same digest; same key different
  digest; same SP different attempt → Existing receipt vs hard reject."** A re-sent identical
  command under a fresh key defeats the "existing receipt" path entirely.
- **`contracts.md` §5 rule 2:** durable history is checked per
  `(binding, revision, idempotency_key)`; a fresh key for an old intent bypasses that lookup.

**(c) Minimal fix**

Back the intent with a `Map<serializedIntent, key>` instead of a single `previous`/`key` pair, so
a returning intent recovers its original key. Cap or scope the map to the component lifetime if
unbounded growth is a concern (these are per-panel `useState` instances, so the natural bound is
small).

Then, defensively, key `VersionControlApi.post`'s `commands` cache on the **intent** as well, so
that even a caller that mismanages keys cannot double-send an identical command. The existing
mismatch rejection at `:50-51` stays as-is.

**(d) RED test to write first**

`lib/version-control-api.test.ts`:

```
interleaved_intents_reuse_the_original_key_when_the_first_intent_returns
```

Asserts:
- `const k1 = intent.key(A); const k2 = intent.key(B); expect(intent.key(A)).toBe(k1)` — the
  direct statement of per-intent stability;
- `expect(k2).not.toBe(k1)` — retaining the existing changed-intent guarantee;
- end-to-end through the API: issue A, then B, then A again via `api.restore(...)` and assert
  `expect(transport).toHaveBeenCalledTimes(2)` — exactly two network commands for two distinct
  intents, no matter the click order.

The call-count assertion is the important one; the key-equality assertions alone could be
satisfied without fixing the double-send if `post` were later refactored.

---

### B5 — `SemanticDiff.comparison` collapses `equal` and `different`; the UI infers equality from an empty item list

**(a) Location**

- `types/version-control.ts:28` — `export interface SemanticDiff { items: DiffItem[]; comparison: 'known' | 'unknown' }`
- `components/version-control/diff.tsx:8` — `{!diff.items.length && <p>No changes in this comparison.</p>}`
- Producer that hard-codes the wrong vocabulary: `components/version-control/demo-api.ts:67`
  (`comparison: status?.stale ? 'unknown' : 'known'`).
- Test that pins the wrong vocabulary: `components/version-control/diff.test.tsx:6` and `:11`
  (`comparison: 'known'` / `comparison: 'unknown'`).
- **Decisive external evidence:** `backend/tests/fixtures/vc_contracts/enums.json` declares
  `"Comparison": ["equal", "different", "unknown"]`. The frontend union does not match M02's
  frozen fixture.

**(b) Defect and invariant violated**

Two coupled problems.

First, the type has **two** states where the contract mandates **three**. `contracts.md` §4 is
verbatim explicit: "`Comparison` distinguishes **equal/different/unknown**, not a misleading
boolean." `'known' | 'unknown'` is exactly the misleading boolean the contract names — it is a
renamed `boolean`, and it discards the equal/different distinction that carries the meaning.

Second, and worse in practice: because `different` is not representable, the component **infers
equality from the absence of items** (`diff.tsx:8`). `items: []` renders the affirmative claim
"No changes in this comparison." But an empty array has several possible causes — a genuinely
equal pair, a truncated or paged diff, a filtered projection, a partially-computed comparison, a
backend that returned early. Rendering "No changes" for all of them is the *same class of error*
as inferring `clean` from `live == observed`: an authoritative equality claim manufactured from
absent evidence. That is the specific failure mode M09 exists to prevent.

Violates:
- **`contracts.md` §4:** "`Comparison` distinguishes equal/different/unknown, not a misleading
  boolean."
- **`contracts.md` §1:** "Unknown canonicalizer comparison produces `unknown` unless both sides
  can be recomputed under one supported version" — a three-state notion the two-state type cannot
  express.
- **M02's frozen `enums.json`** (`Comparison: ["equal","different","unknown"]`) — this is
  concrete type drift against a shared fixture, which is precisely what task 1 was supposed to
  catch (see B6).
- **`testing-strategy.md` F-CANONICALIZER: "Distinct fingerprints; **unknown instead of false
  equality**."**

Credit where due: the `unknown` branch itself is handled well — `diff.tsx:3` returns
"Unknown comparison — evidence is unavailable; not proof of equality." and `diff.test.tsx:12`
asserts `not.toContain('No changes')` for it. The fix generalises that existing correct instinct
to the `different`-with-no-items case.

**(c) Minimal fix**

1. `types/version-control.ts:28` → `comparison: 'equal' | 'different' | 'unknown'`, matching
   `enums.json`.
2. `diff.tsx` → render "No changes" **only** when `comparison === 'equal'`. For
   `comparison === 'different'` with `items.length === 0`, render an explicit
   "comparison incomplete — differences reported but no items returned" state, which is an
   evidence-gap message, not an equality claim.
3. Update the demo producer (`demo-api.ts:67`) to emit `'equal' | 'different' | 'unknown'`.

**(d) RED test to write first**

`components/version-control/diff.test.tsx`:

```
diff_never_infers_equality_from_an_empty_item_list
```

Asserts:
- `{comparison:'different', items:[]}` → does **not** contain "No changes"; contains the
  incomplete-comparison message;
- `{comparison:'equal', items:[]}` → **is** the only input that may render "No changes";
- `{comparison:'unknown', items:[]}` → keeps the existing "Unknown comparison" wording and still
  does not contain "No changes" (regression guard on `diff.test.tsx:11-12`).

Additionally, the type change should be *forced* by the B6 fixture test rather than only by this
component test — see `every_drift_and_operation_enum_...` below, which parses `enums.json` and
will fail to compile/assert against a two-state union.

---

### B6 — Task-1 contract test is one inline literal; fixtures are M09-authored TypeScript rather than M02 JSON; no enum, approval, or receipt coverage

**(a) Location**

- `lib/version-control-api.test.ts:6-24` — `loads_vc_contract_fixtures_without_type_or_nullability_drift`
  in full. It defines one inline `fixture` object (`:7-13`), round-trips it through
  `api.overview()` (`:16`), and asserts two request URLs (`:17`, `:19-23`).
- `components/version-control/fixtures.ts:1-36` — the entire fixture module: hand-authored
  TypeScript literals owned by M09.
- Demo status matrix with gaps: `components/version-control/demo-api.ts:140-150` — nine rows
  covering `diverged`, `conflicted`, `unknown`, `unreachable`, `applied_unverified`; **absent**:
  `clean`, `external_ahead`, `desired_ahead`.
- **The fixtures that should have been consumed and were not:**
  `backend/tests/fixtures/vc_contracts/` — 15 JSON files, present in the repository *today*:
  `enums.json`, `binding_status.json` (8 examples, one per `DriftState`),
  `version_summary.json` (6 examples, one per `Origin`), `operation_handle.json` (12 examples,
  one per `OperationStatus`), `approval_inputs.json` (2), `deployment_receipt.json` (2),
  `diff_item.json` (33), `api_error.json` (2), `observation_result.json`,
  `package_manifest.json`, `fingerprints.json`, `heads.json`, `binding_ref.json`,
  `observation_ref.json`, `create_intent_ref.json`.

**(b) Defect and invariant violated**

The test named to prove "no type or nullability drift" round-trips **a single hand-written
`OverviewPage` literal that the same test file authored**. A test whose input and expectation are
both written by the consumer cannot detect drift from the producer — it is a self-consistency
check. It never touches the 8 `DriftState` values, the 12 `OperationStatus` values,
`ApprovalRecord`, `DeploymentReceipt`, `PackageManifest`, `VersionDetail`, or `SemanticDiff`.

And `fixtures.ts` is M09-authored TypeScript, not M02's shared JSON. Because the fixtures were
authored rather than consumed, **real drift shipped undetected**. Comparing the diff's types
against the frozen JSON now in the repo:

| Field | M02 frozen JSON | M09 TypeScript in the diff | Consequence |
|---|---|---|---|
| `Comparison` | `["equal","different","unknown"]` (`enums.json`) | `'known' \| 'unknown'` (`types/version-control.ts:28`) | B5 — equality inferred from empty list |
| `ApprovalInputs.target_binding` | full `BindingRef` object (`approval_inputs.json`) | `string` (`types/version-control.ts:39`) | comparisons like `approval.inputs.target_binding !== selection?.target_binding` (`promotion.tsx:23`) compare an object to a string → always unequal, or always equal once coerced. A promotion gate silently mis-evaluates. |
| `DeploymentReceipt.target_binding` | full `BindingRef` object (`deployment_receipt.json`) | `string` (`types/version-control.ts:53`) | same class |
| `ApprovalInputs.recovery_policy` | object `{automatic_clear, residual_risk_acknowledged}` | `string` (`types/version-control.ts:45`) | recovery policy unreadable |
| `ApprovalInputs.artifact_digest` / `mapping_digest` / `transformer_version` / `permission_policy_digest` | present-and-`null` | optional `?:` (`types/version-control.ts:40-43`) | `'x' in inputs` and `toEqual` checks diverge from the wire |
| `ApiError.details` | present (`api_error.json`) | absent from the interface (`types/version-control.ts:31`) | contracts §7 `ApiError(..., details?)` unmodelled |
| `Fingerprints.canonicalizer_version` | `"vc-c14n/1"` | `'VC/1.0'` in `fixtures.ts:3` | seam version conflated with canonicalizer version |

The `target_binding` rows are not cosmetic: `promotion.tsx:23` and `reconcile.tsx:22` both gate on
`target_binding` equality, so a string/object mismatch means a **security-relevant gate that
cannot evaluate correctly against real backend payloads.**

Violates:
- **`contracts.md` §8: "M02 supplies JSON fixtures for nullable fields, stale status, **all**
  drift/operation enums, fingerprints, **full approval and receipt payloads** … tests check Python
  serialization against TypeScript fixture consumption."** None of this happens.
- **Module plan §7: "Backend response fixtures cover three distinct heads, external origin,
  invalidated approval, partial description write, **ambiguous create**, unreachable history and
  target receipt."** Ambiguous create and several drift states are absent from the demo matrix.
- **Module plan §2 / `testing-strategy.md` §5: "New shared fakes/DTOs go through M02"** — M09 may
  *request* fixture additions but must not author the shared contract fixtures. Authoring them
  locally is what removed the drift signal.
- **Module plan §5 task 1** is therefore not satisfied despite the test bearing its name.

**(c) Minimal fix**

1. Import M02's JSON directly in the test — e.g.
   `import enums from '../../../backend/tests/fixtures/vc_contracts/enums.json'` (Vite/Vitest
   resolve JSON natively; add `resolveJsonModule` to `tsconfig.app.json` if the compiler objects).
   Read-only consumption of M02's folder is explicitly permitted by module plan §2
   ("Read only: … shared M02 JSON fixtures"). Do **not** edit that folder.
2. Fix the seven drift rows in `types/version-control.ts` to match the JSON — in particular
   introduce a `BindingRef` interface and use it for both `target_binding` fields, then update the
   two gates (`promotion.tsx:23`, `reconcile.tsx:22`) to compare `binding_id` **and**
   `binding_revision` explicitly.
3. Re-express `fixtures.ts` as thin typed views over the imported JSON, so a future producer
   change breaks the build rather than passing silently.
4. Where a needed case is genuinely missing from M02 (ambiguous create; `clean` /
   `external_ahead` / `desired_ahead` in a *paged overview* shape), **request the addition from
   M02** per module plan §7 and record the request. Do not add it to M02's folder from M09.

**(d) RED test to write first**

`lib/version-control-api.test.ts`, replacing the body of the existing task-1 test rather than
sitting beside it (the current single-literal assertion should be deleted, not kept as a passing
decoy):

```
every_drift_and_operation_enum_and_full_approval_receipt_payload_parses_from_m02_json_fixtures
```

Asserts:
- `enums.json`'s `DriftState`, `OperationStatus`, `Origin`, `Comparison`, `DiffCategory` and
  `DiffChange` arrays each round-trip through the corresponding TS union with an exhaustiveness
  switch (a `default: assertNever(x)` so an added or renamed member is a **compile** error);
- every example in `binding_status.json` (8), `version_summary.json` (6) and
  `operation_handle.json` (12) parses into the typed DTO through the real
  `VersionControlApi` + a fake transport;
- `approval_inputs.json` and `deployment_receipt.json` examples parse, with
  `expect(Object.keys(parsed).sort()).toEqual(EXPECTED_FIELDS.sort())` against the `contracts.md`
  §6 field list — this is the assertion that catches the `target_binding` and `recovery_policy`
  shape drift;
- `api_error.json`'s `details` survives parsing;
- nullability: fields that M02 emits as present-and-`null` are asserted present
  (`expect('artifact_digest' in inputs).toBe(true)`), not merely undefined-tolerant.

This one test, written first, fails on **seven** distinct type errors and thereby drives the B5
type fix as well.

---

### B7 — The blind-retry assertion is structurally incapable of failing, and `canMutate`'s six safety predicates are untested

**(a) Location**

- `components/version-control/reconcile.test.tsx:19-30` —
  `quarantine_and_partial_outcome_never_offer_blind_retry`, in particular the negative assertion at
  `:25`:
  ```ts
  expect(html).not.toMatch(/>Retry|>Reapply|>Restore|>Promote/)
  ```
  asserted against `OperationStatusView`.
- The component it is asserted against: `components/version-control/reconcile.tsx:8-19`
  (`OperationStatusView`) — which renders exactly **one** button, "Verify operation status"
  (`reconcile.tsx:17`), in every code path.
- The untested gate: `hooks/use-version-control.ts:12` — `canMutate`, a single boolean expression
  encoding six independent safety predicates.
- Its only coverage: `components/version-control/version-control-tab.test.tsx:93` — a loop over
  `bindingFixture.allowed_actions` in the *503/stale* scenario only.
- Same vacuous-negative pattern, for the record: `version-control-tab.test.tsx:100`
  (`expect(html).not.toContain('Request adoption approval')` asserted against
  `VersionControlView`, which never renders that string in any branch) and
  `history.test.tsx:8` (`expect(html).not.toContain('Version 3')` — `History` has no code path
  that could emit it).

**(b) Defect and invariant violated**

Two problems, one root cause.

**The named test cannot fail.** `OperationStatusView` is a static evidence view whose only
control is hard-coded to the string "Verify operation status". No possible implementation of that
component — correct or broken — would emit `>Retry`, `>Reapply`, `>Restore` or `>Promote`. The
regex therefore passes vacuously for all four statuses in the loop. Deleting the assertion changes
nothing; inverting the component's behaviour changes nothing. Per `testing-strategy.md` §1, "An
import/syntax/environment error is not a sufficient behavioral red once scaffolding exists" — an
assertion that no implementation can violate is the same defect one step further along: there was
never a red state.

The genuinely valuable half of that test — `expect(html).toContain(...)` for the status,
checkpoints and audit strings at `:22-24`, and "No mutation replay" at `:26` — *is* meaningful and
should be kept. It is only the negative-space claim that is empty, and the negative-space claim is
the one the invariant actually rests on.

**Meanwhile the real gate is untested.** `canMutate` (`hooks/use-version-control.ts:12`) is the
**single** authority for every mutating control in the module — restore, adopt, reapply,
acknowledge, promote, approval request and vote all route through it
(`reconcile.tsx:38`, `:52`; `version-control-tab.tsx:44`, `:47`, `:48`; and via the `disabled`
prop into `RestorePanel`/`PromotionPanel`/`ApprovalsPanel`). It encodes six server-driven
blocking conditions:

```ts
state.captured && !state.loading && !state.busy && !state.stale
  && state.status && !state.status.stale && !state.status.quarantined
  && !state.status.unresolved_operation_id
  && !['unknown','unreachable','applied_unverified','conflicted'].includes(state.status.drift)
  && state.status.allowed_actions.includes(action)
```

Only the outer `state.stale` path is exercised. `status.quarantined`,
`status.unresolved_operation_id`, `status.stale`, `busy`, and each of the four blocking drift
states are **entirely unasserted**. A refactor that dropped the `quarantined` clause, or that
mistyped one of the four drift strings, would ship green.

Violates:
- **Module plan §5 task 10: "`quarantine_and_partial_outcome_never_offer_blind_retry` … Read-only
  verification/status view"** — the named test does not constrain the behaviour it names.
- **`testing-strategy.md` §1** (red before green; a non-failing assertion is not a red) and **§3**
  ("Add the next boundary/failure test… Do not write the whole module and retrofit tests").
- **Module plan §6 AC "FM-OUTAGE/FM-RECOVERY … no blind retries"** — the claim is unverified for
  every blocking condition except stale.

**(c) Minimal fix**

1. Re-point the negative assertion at the components that **actually own** mutating controls.
   `ReconcilePanel`, `RestorePanel` and `PromotionPanel` all render real mutating buttons whose
   `disabled` state is observable in static markup — `version-control-tab.test.tsx:46` already
   proves `expect(controls).toContain('<button disabled=""')` works under
   `renderToStaticMarkup`. Assert against those, not against the evidence view.
2. Table-test `canMutate` directly, one row per predicate.

Both are test-only changes; if `canMutate` is already correct they will pass immediately, which is
the point — the current suite cannot tell us whether it is.

**(d) RED tests to write first**

Rewrite the existing vacuous assertion, then add:

`hooks/use-version-control.test.ts`:

```
can_mutate_is_false_for_quarantined_unresolved_and_each_blocking_drift_state
```

A table with one row per predicate, each starting from a fully-permissive baseline state and
flipping exactly one field, asserting `false`: `status.quarantined: true`;
`status.unresolved_operation_id: 'op-1'`; `status.stale: true`; `busy: true`; `captured: false`;
`loading: true`; and `drift` set to each of `'unknown'`, `'unreachable'`,
`'applied_unverified'`, `'conflicted'`. Plus one positive control asserting the permissive
baseline returns `true`, and one asserting an action absent from `allowed_actions` returns `false`.
The positive control is what makes the negatives meaningful — without it, `canMutate` returning
constant `false` would pass every row.

`components/version-control/reconcile.test.tsx`:

```
reconcile_and_restore_controls_render_disabled_for_quarantined_and_partial_bindings
```

Render `ReconcilePanel`, `RestorePanel` and `PromotionPanel` with a `quarantined` status and again
with an `unresolved_operation_id` status, and assert **every** mutating button in the markup
carries `disabled=""` — counting them, so that a future button added without gating fails the
count rather than slipping past a substring check.

---

### B8 — HTTP 409 / 423 / 503 collapse into one `stale` boolean and one "Stale history" alert; only 503 is tested

**(a) Location**

- `hooks/use-version-control.ts:33-36` — the catch block in `open`:
  ```ts
  const label = error instanceof VersionControlError
    ? error.status === 423 ? 'Quarantined / unresolved: '
    : error.status === 409 ? 'Conflicted; preserved history: '
    : error.status === 503 ? 'Evidence unreachable: ' : '' : ''
  updateCurrent({ captured: false, stale: true, error: label + (...) })
  ```
- `components/version-control/version-control-tab.tsx:20` — the single rendered surface:
  ```tsx
  {state.stale && <p role="alert">Stale history — mutating actions disabled. {state.error}</p>}
  ```
- The state shape that cannot represent three outcomes:
  `hooks/use-version-control.ts:7-10` (`VersionControlState`), field `stale: boolean`.
- Coverage: `components/version-control/version-control-tab.test.tsx:85-97`
  (`capture_failure_shows_stale_history_and_disables_mutating_actions`) — a 503 only
  (`:86-88`). No test issues a 423 or a 409 on open.

**(b) Defect and invariant violated**

All three failure classes set the **same** `stale: true` flag, and the view renders **one** alert
whose leading text is "Stale history — mutating actions disabled." The only differentiator is a
prose prefix embedded inside a sentence that mislabels the condition: a quarantined binding is
announced to the operator as *stale history*. A 409 conflict — where the backend has explicitly
**preserved external state** and the correct operator action is to inspect and reconcile — is
likewise announced as stale history.

Module plan §3 does not describe one degraded mode with three sub-messages; it specifies three
distinct presentations with different operator affordances:

> "409 → show preserved conflict; 423 → unresolved/quarantined, **no mutation retry button**;
> 503 → stale/fail-closed, **history remains usable if available**."

These differ in what the UI must *do*, not merely what it says. And `contracts.md` §7 reinforces
the 503 case specifically: "A 503 does **not** invite mutation replay; clients poll the operation
id" — the 423 path in particular should surface the `operation_id` off the `ApiError` (which
`VersionControlError` already carries, `lib/version-control-api.ts:5`, and which is populated in
M02's `api_error.json`) as a drill-through, not bury it in a string.

Violates:
- **Module plan §3 API status rules** (three distinct presentations) — collapsed to one.
- **Module plan §6 AC "FM-OUTAGE/FM-RECOVERY: stale/unreachable/partial/quarantine states
  distinguishable"** — this is the second, independent mechanism by which that AC fails (B1 is the
  projection/badge path; this is the capture-failure path).
- **`testing-strategy.md` FM-OUTAGE, frontend column: "Stale state disables commands but displays
  readable history"** — satisfied for 503, unverified for the other two.

Credit: setting `captured: false` and `stale: true` on *every* failure is correctly fail-closed —
no failure mode accidentally enables mutation. The defect is loss of operator information, not
loss of safety. The fix must preserve the fail-closed default for all three.

**(c) Minimal fix**

Replace the `stale: boolean` field with a discriminated failure value on `VersionControlState`:

```ts
captureFailure: { kind: 'conflict' | 'unresolved' | 'unavailable'; message: string; operationId?: string } | null
```

Derive `kind` from the HTTP status (409 / 423 / 503 respectively; anything else →
`'unavailable'`), and keep a derived `stale` getter if call sites need it so `canMutate`'s
fail-closed logic is unchanged. Then render three distinct surfaces in `VersionControlView`:
preserved-conflict (offers compare, not mutate), unresolved/quarantined (offers the
`operation_id` drill-through into `OperationStatusView` — shares the fix from B1 — and explicitly
no retry control), and stale/unavailable (keeps history readable, offers refresh).

**(d) RED test to write first**

`components/version-control/version-control-tab.test.tsx`:

```
open_distinguishes_409_conflict_423_unresolved_and_503_stale_surfaces
```

Three transports returning 409, 423 and 503 on `/observe` respectively, each with a populated
`ApiError` body (use `backend/tests/fixtures/vc_contracts/api_error.json` as the payload shape —
this is why B6 lands before B8). For each, assert:

- the three rendered markups are **pairwise distinct** (same `new Set(...).size` technique as B1);
- the 409 surface names the preserved conflict and does **not** say "Stale history";
- the 423 surface names quarantine/unresolved **and** exposes the `operation_id` from the error
  body, and contains no retry control;
- the 503 surface keeps the history list readable — reuse the existing strong assertions at
  `version-control-tab.test.tsx:94-96` (`state.history.items` retained, `'external-3'` present in
  the markup);
- and for all three, `canMutate(state, action) === false` for every action in
  `allowed_actions` — preserving the fail-closed property that the current code gets right.

---

# Non-blocking findings

These do not gate the module. Items 9–13 are ordered as raised in the original review.

**9. `aria-live="polite"` is scoped to the entire panel.**
`components/version-control/version-control-tab.tsx:17` puts `aria-live="polite"` on the outer
`<section>` wrapping the heading, badge, head list and the whole history list. Every state change
re-announces the full subtree, which in practice makes the live region unusable for the operator it
is meant to help. Scope the live region to the status/error paragraphs (`:18-21`) instead. The
`role="status"` / `role="alert"` usage throughout the module is otherwise correct and is what
`version_control_actions_are_keyboard_accessible_and_errors_announced` genuinely verifies
(`version-control-tab.test.tsx:38-40`).

**10. No-op assertion at `version-control-tab.test.tsx:24`.**
```ts
expect(JSON.parse(String(transport.mock.calls.filter(([url]) => String(url).endsWith('/observe')).length))).toBe(2)
```
`JSON.parse(String(2))` is just `2`; the round-trip is inert. The assertion is *correct* — it does
check that two observes were issued — but the wrapping is meaningless and signals the line was
written without being executed in a red state. Write
`expect(observeCalls).toHaveLength(2)`. Cosmetic, but worth fixing precisely because of what it
implies about the red step.

**11. `OverviewBadge` short IDs — verified correct, no action.**
`components/version-control/overview-badge.tsx:12` renders
`<dd title={version ?? undefined}>{version?.slice(0, 8) ?? 'Not recorded'}</dd>`. The truncation is
display-only, the full value is preserved in `title`, `'Not recorded'` is distinct from any real
ID, and nothing anywhere derives ordering from these strings. This satisfies `contracts.md` §8
("Short IDs are display labels, never monotonic version numbers or authoritative ordering") and
module plan §8 ("invent numeric version ordering from UUIDs"). Recorded here so a later reviewer
does not re-flag it — and so the B1 fix, which edits this same render, does not regress it.

**12. `VersionDetail.snapshot?: unknown` is unusable as written.**
`types/version-control.ts:20` — `export interface VersionDetail extends VersionSummary { snapshot?: unknown }`.
`unknown` does not violate module plan §3 (which forbids `any`), and `api.version()`
(`lib/version-control-api.ts:63`) is never called anywhere in the diff — so this is inert rather
than wrong. But no consumer can read the field without an unchecked cast, which is where `any`
creeps back in later. Either model the `contracts.md` §1 `Snapshot` shape (envelope, parsed
document, allowlisted restorable metadata, raw digests, canonical representations, fingerprints) or
drop the field until a consumer exists. Note `contracts.md` §1 also warns that snapshots must not
be exposed to unauthorized viewers, so modelling it invites an authorization question this module
should answer deliberately rather than by accident.

**13. Two independent `ApprovalsPanel` instances can request competing approvals.**
`components/version-control/promotion.tsx:112` renders an `ApprovalsPanel` inside
`PromotionPanel` (keyed by `releaseId`), while `components/version-control/version-control-tab.tsx:47`
renders a **second, independent** `ApprovalsPanel` for the reconcile flow. They share no state.
An operator can request one approval in each, and there is no indication which approval
`promote` will consume — `PromotionPanel` reads only its own `approval` state (`promotion.tsx:23`,
`:114`). No invariant is broken, because the server re-validates the approval against the release
inputs (`demo-api.ts:112` in the fake; `contracts.md` §6 in the contract), so a mismatched approval
is rejected rather than misapplied. But it is a real operator-confusion path, and the two panels
will diverge further as the flows grow. Consider hoisting approval state to a single owner.

---

# Closing note on TDD depth

This was my top scrutiny point, and the honest finding is that the problem is **not** simply
"several files carry only 1–2 test cases." Case count is a poor proxy here: the module's two best
tests are single-case, and some multi-assertion tests constrain nothing. The real distribution:

**Genuinely strong — assert exact URLs, call counts, request bodies, and real negative space:**

| Test | Location | Why it is strong |
|---|---|---|
| `fake_api_supports_..._without_network` | `version-control-tab.test.tsx:57-82` | Stubs global `fetch` to throw, drives nine sequential operations, asserts `not.toHaveBeenCalled()`. Proves the no-network claim rather than asserting it. |
| `overview_does_not_fetch_live_genie_per_row` | `overview-badge.test.tsx:21-30` | 25 rows → `toHaveBeenCalledTimes(1)`; exact cursor URL; `every(url => includes('/overview?'))`. |
| `late_response_for_previous_binding_cannot_replace_current_heads` | `use-version-control.test.ts:6-23` | Real interleave with a held promise; asserts the stale response cannot overwrite `status`, `heads` **or** `history`. |
| `promotion_requires_explicit_target_mapping_preflight_and_approval` | `promotion.test.tsx:8-27` | Asserts the full ordered URL sequence (`:18`), exact bodies, and four distinct rejection paths incl. post-hoc selection change (`:25-26`). |
| `receipt_shows_intended_rendered_observed_and_compensation_status` | `promotion.test.tsx:29-37` | `not.toContain('Deployment confirmed')` against a partial receipt — a real negative claim; asserts GET-only polling (`:36`). |
| `double_click_or_network_loss_...` | `version-control-api.test.ts:25-42` | Strong as far as it goes — call-count assertions and the changed-intent rejection. Its gap is the A→B→A interleave (B4), not its rigour. |

**Structurally incapable of failing — no implementation could violate them:**

| Assertion | Location | Why it cannot fail |
|---|---|---|
| `not.toMatch(/>Retry\|>Reapply\|>Restore\|>Promote/)` | `reconcile.test.tsx:25` | Asserted against `OperationStatusView`, which renders exactly one hard-coded button. **B7.** |
| `not.toContain('Request adoption approval')` | `version-control-tab.test.tsx:100` | Asserted against `VersionControlView`, which has no code path emitting that string (it lives in `ReconcilePanel`, `reconcile.tsx:52`). |
| `not.toContain('Version 3')` | `history.test.tsx:8` | `History` has no numeric-label code path at all. The intent — "no invented version ordering" — is right; the assertion tests nothing. |
| Three `toContain` checks on header copy | `version-control-tab.test.tsx:50-53` | `workbench_exposes_an_explicit_api_faked_entry_point` checks static prose in `VersionControlWorkbench.tsx:12-13`. A documentation check, not behavioural. |

**Asserts the defect — a correct fix would be scored as a regression:**

| Assertion | Location | Problem |
|---|---|---|
| `toEqual(approvalInputsFixture)` | `reconcile.test.tsx:14` | Requires the client to POST the hand-authored fixture **verbatim**, including `requester_id: 'demo-requester'` and `preflight_evidence_digest: 'preflight-1'`. Deriving these from server evidence — the fix — breaks this test. **Must be rewritten first (B3).** |

**Coverage gap in the badge matrix.** `overview_badge_uses_approved_deployed_not_observed_equality`
(`overview-badge.test.tsx:7-19`) exercises 4 of the 8 `DriftState` values — `unknown`,
`unreachable`, `applied_unverified`, `conflicted` (`:12`), plus the three `clean` policy branches
(`:9-11`). It **omits `external_ahead`, `desired_ahead` and `diverged`** — the three most common
real-world states, and the three whose labels an operator will see most often. The demo status
matrix (`demo-api.ts:140-150`) has the mirror-image gap: it covers the exotic states and omits
`clean`, `external_ahead` and `desired_ahead`. M02's `binding_status.json` supplies all eight, one
example per state, which is why B6's fixture consumption closes this gap as a side effect.

**Interpretation.** The API/transport layer (`lib/version-control-api.ts`,
`hooks/use-version-control.ts`, the demo transport) shows every sign of having been built
test-first: exact-URL assertions, call-count assertions, held-promise interleaves, negative-space
claims. The component layer shows the opposite signature — render-and-grep-for-a-substring, with
negative assertions aimed at components that could never emit the forbidden string. That is the
pattern `testing-strategy.md` §3 prohibits ("Do not write the whole module and retrofit tests"),
and it is the direct cause of B1, B2, B5 and B8 surviving into the diff: each is a *rendering or
flow* defect in exactly the layer whose tests cannot fail. B6 is the same story one level up —
task 1 was named for drift detection but written as self-consistency, so seven real type drifts
against M02's frozen fixtures shipped unnoticed.

The remedy is not more test cases. It is, for each of the eight findings, one assertion that fails
against the current code and passes only against the fixed code — which is what each **(d)**
section above specifies.
