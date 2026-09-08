# M07 fix2: close 2 source-leak regressions introduced by the blocker-fix (re-review BLOCK)

Fix in the EXISTING M07 worktree /home/sandbox-agent/workspace/worktrees/m07 (branch vc/m07, HEAD 9271dbc, clean; pushed). Repo venv: /home/sandbox-agent/workspace/databricks-genie-workbench/.venv/bin/python -m pytest ... from the worktree root. STRICT TDD: write the RED test FIRST for each finding, confirm meaningful failure against current HEAD, implement minimum, commit after EACH. Owned-paths: ONLY backend/services/version_control/promotion/mapping.py + backend/tests/test_vc_mapping.py. Do NOT touch anything else.

The M07 blocker-fix RESOLVED all 4 original blockers (B1/B3/B4 fully, B2 partial) but the independent re-review (differentially executed pre-vs-post) found the B1 and B2 fixes each introduced a NEW source-leak regression. Both are real (proven by execution) and must be closed before M07 integrates.

## F1 — SQL prefix mapping lets a blessed catalog mapping promote arbitrary unreviewed source tables (B1 regression via the SQL path)
Location: promotion/mapping.py ~95-98 and ~113-114.
Invariant: every source identifier reaching target SQL must be covered by an EXACT reviewed mapping entry. The fix replaced exact membership (`identifier not in mappings`) with LONGEST-PREFIX matching, so a one-token mapping authorizes an unbounded subtree.
Evidence (data_sources.catalogs is a first-class allowlisted path, so {'dev':'prod'} is a supported mapping):
  mappings={'dev':'prod'}; sql='SELECT * FROM dev.internal.pii_raw'
  PRE: raised 'Unresolved relation requires an exact reviewed mapping'
  POST(shipped): PROMOTED -> 'SELECT * FROM prod.internal.pii_raw'  (never reviewed table leaks)
Also reachable via the existing catalog+schema test's mapping set (dev.sales.customers_UNREVIEWED -> prod.reporting.customers_UNREVIEWED). The `target_prefix` arm is identically affected. Secondary: the same edit relaxed `len(parts) != 3` -> `len(parts) < 3`, and relation_pending's guard now passes on any prefix hit.
FIX (reviewer-verified in scratch — closes the leak AND keeps the new join-fragment test green): restrict prefix extension to FULLY-QUALIFIED 3-part sources (column-qualification only, which is all the join-fragment case needs), and restore the exact 3-part length check:
```python
mapped_prefix = next((source for source in sorted(identifier_mappings, key=len, reverse=True)
                      if identifier == source
                      or (len(source.split('.')) == 3 and identifier.startswith(source + '.'))), None)
```
Apply the same predicate to the `target_prefix` arm, and restore `len(parts) != 3` (from the relaxed `< 3`).
RED test (test_vc_mapping.py): test_sql_prefix_mapping_requires_exact_or_three_part_source — mappings={'dev':'prod'}, example_question_sqls[0].sql=['SELECT * FROM dev.internal.pii_raw']; assert pytest.raises(ValueError). Fails as shipped (promotes prod.internal.pii_raw), passes with the tightened predicate. (Keep/verify the fix's own new join-fragment column-qualification test still green.)

## F2 — join_specs.sql element >= 1 is an unvalidated verbatim passthrough (B2 regression)
Location: promotion/mapping.py ~141-142.
Invariant: no bytes reach the target space without either mapping or fail-closed validation. Arity preservation was implemented as SKIP ALL PROCESSING for index > 0, so element 1 bypasses the token scanner entirely — never checked for source identifiers, statement shape, or ';'.
Evidence:
  elem1='SELECT * FROM dev.internal.pii_raw'  PRE: raised   POST: PROMOTED verbatim
  elem1='DROP TABLE prod.sales.orders'        PRE: raised   POST: PROMOTED verbatim
  elem1='--rt=...MANY_TO_ONE--\nUNION SELECT * FROM dev.internal.secrets'  PRE: raised  POST: PROMOTED verbatim
  3-element join sql: element 2 also passed through unchecked
Byte-identical preservation is only CORRECT for the `--rt=` annotation schema.md mandates; as shipped it is unconditional.
FIX (validate rather than skip — require the trailing element to be exactly the annotation, reject extra elements):
```python
if join_fragment and index > 0:
    if index > 1 or not re.fullmatch(r'--rt=FROM_RELATIONSHIP_TYPE_[A-Z_]+--', fragment_sql):
        raise ValueError('join_specs.sql requires condition plus reviewed --rt-- annotation')
    rendered.append(fragment_sql)
```
(Confirm the exact annotation grammar against backend/references/schema.md:204 — adjust the regex if schema.md specifies a different exact form; the intent is: element 0 is the mapped condition, element 1 must be EXACTLY the reviewed --rt-- annotation, and there must be no element 2+.)
RED test (test_vc_mapping.py): test_join_spec_sql_trailing_elements_are_validated_not_passthrough — parametrize join_specs[0].sql[1] over ['SELECT * FROM dev.internal.pii_raw','DROP TABLE prod.sales.orders','--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--\nUNION SELECT 1'] -> pytest.raises(ValueError); plus a 3-element case -> raises. All four promote as shipped (RED today). Keep the valid 2-element [condition, '--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--'] case GREEN (identifiers in elem0 mapped, elem1 byte-identical).

## Non-blocking (address ONLY if trivial and within mapping.py/test file; otherwise note in report)
- NB1: comma-relation guard no longer spans array elements (mapping.py ~138-146): `['SELECT ... dev.sales.orders\n', ', dev.sales.customers\n']` promotes where PRE raised 'Comma relations require reviewed explicit JOIN syntax'. Not a leak (both exactly-mapped; unmapped still raises) but the reviewed-JOIN intent is evadable by splitting at the comma. Consider scanning the joined element stream for comma-relations, or note as backlog.
- NB2: whole-array `sql:` overrides silently stop applying to multi-element SQL (PRE joined before override lookup; POST per-element) -> fail-closed but any existing reviewed multi-element override must be re-keyed. Add a one-line migration note in the report (do not change behavior unless trivial).

## FINAL
Run from repo-root context: `python -m pytest backend/tests/test_vc_mapping.py -q` then `python -m pytest --ignore=backend/tests/integration -q` — GREEN, ZERO regressions (baseline was 1158 with M07). Confirm the M07 integration test stays marked+deselected. Report per finding: RED->GREEN evidence, files changed, confirmation the fix's own prior tests (B1 allowlist, B2 valid join fragment) stay green, final pass count, owned-paths confirmation, and the NB2 migration note.
