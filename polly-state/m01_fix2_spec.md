# M01 Fix #2 — residual false-drift in data_sources normal form (BLOCKING)

Cross-vendor re-review (claude_code) confirmed B-1 and B-2 are correctly fixed, but
found ONE new BLOCKING residual false-drift path introduced by the B-2 merge itself.
Fix via STRICT TDD (RED test first).

## The bug
File: `backend/services/config_fingerprint.py` around line 302-303.

The B-2 normal form splits `data_sources` entries into:
- `by_identifier` — entries carrying a string identity key (deduped/sorted), and
- `unaddressed` — entries WITHOUT a string identity key (appended raw).

Currently the code concatenates `by_identifier` values + `unaddressed` and hands the
combined list to `canonicalize`. But `canonicalize`'s uniqueness/isinstance(str) guard
(~line 452) requires EVERY element to carry a string identity key. A single unaddressed
entry makes the `all(...)` guard fail, so NO sorting occurs and the addressed entries
keep raw input order.

Result (false drift):
```
submitted [a, b, {"name": "no-identifier"}] -> sources [a, b, no-identifier]
readback  [b, a, {"name": "no-identifier"}] -> sources [b, a, no-identifier]  # config hash DIFFERS
```
This is exactly the false-drift class B-2 exists to eliminate, and it triggers precisely
when Genie readback returns a malformed/partial source entry. Violates acceptance
criterion "API normalization does not trigger false drift."

## Required fix
Sort the addressed group independently and append unaddressed AFTER, so unaddressed
entries cannot perturb addressed ordering:
```python
sources["sources"] = [
    *(by_identifier[key] for key in sorted(by_identifier)),
    *unaddressed,
]
```
(Adjust variable names to match the actual code. The key point: sort the addressed
group by its identity key before concatenation; do NOT rely on the downstream
`canonicalize` guard to sort a mixed list.)

## Required RED test (write FIRST, confirm meaningful failure, then fix)
Add to `backend/tests/test_config_fingerprint.py` a test with a mixed
addressed/unaddressed `data_sources` set, reordering ONLY the addressed entries between
submitted and observed, plus at least one identifier-less entry present in both, and
assert `fingerprints.config` (or the semantic-equality result) is EQUAL across the
reorder. Name it clearly, e.g.
`test_vc_data_sources_addressed_order_independent_with_unaddressed_entry`.
Confirm it FAILS against current code (drift detected), then apply the fix and confirm GREEN.

## Non-blocking (do NOT fix now unless trivial; just note in report)
- Finding 2: legacy `_collection_keys=None` path hash changed for DUPLICATE-id arrays
  (uniqueness guard added to shared canonicalize). Either scope the guard to the VC path
  (`_collection_keys is not None`) OR soften the "frozen for compatibility" docstring at
  ~lines 25-28/401 to "frozen except duplicate-id arrays" + add a characterization test.
  If the one-line guard-scoping is trivial and safe, prefer it.
- Finding 3: `_strip_internal_keys` is top-level-only; nested internal keys survive.
  Likely intentional (avoid silently masking unknown fields per section 8). DOCUMENT the
  decision; do not change behavior without justification.
- Finding 4: non-MV type markers asymmetric (only METRIC_VIEW dropped). Correct per doc
  scope; just flag to M04/M10.

## Constraints
- OWNED PATHS ONLY: backend/services/config_fingerprint.py; new
  backend/services/version_control/canonical_diff.py; backend/tests/test_config_fingerprint.py;
  backend/tests/test_vc_semantic_diff.py; backend/tests/fixtures/vc_canonicalization/.
- Worktree: /home/sandbox-agent/workspace/worktrees/m01 (branch vc/m01, HEAD e3190bf).
- Commit incrementally (git add -A && git commit) after the RED test lands and after the
  fix goes GREEN, so a 429 can't wipe progress.
- Verify green: python -m pytest backend/tests/test_config_fingerprint.py backend/tests/test_vc_semantic_diff.py -q
- Report: commits (sha+msg), exact test count, which non-blocking items you addressed vs
  deferred, owned-path compliance.
