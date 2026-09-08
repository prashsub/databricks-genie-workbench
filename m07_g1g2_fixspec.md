# M07 G1/G2 Fixspec (from cross-review of vc/m07-astra @ d1a489e)

VERDICT was BLOCK. B1, B3, B4, F1 are CONFIRMED RESOLVED (RED-verified) — do NOT regress them.
Only G1 and G2 remain. Both are in `backend/services/version_control/promotion/mapping.py`.
Both are genuine SOURCE-LEAK / statement-class-bypass defects introduced/left by the B2 and F2 fixes.

Owned-paths for this fix: backend/services/version_control/promotion/mapping.py + backend/tests/test_vc_mapping.py (and test_vc_promotion/test_vc_packages/test_vc_dispatch only if needed). NOTHING else.

---

## BLOCK G1 — B2's `fragment_mode` skips the post-concatenation recheck, restoring a silent source leak

**Location:** `backend/services/version_control/promotion/mapping.py:150-152`

```python
# Validate statement-wide guards across clause-array boundaries too.
if not fragment_mode:
    map_sql(''.join(rendered), mappings)
```

**Why wrong:** Genie concatenates `sql` array elements (`_FRAGMENT_ARRAY_KEYS = {"content", "sql"}` in `config_fingerprint.py:60` — contract is concatenation). For snippets and join specs, `fragment_mode` is True, so the joined-string recheck is SKIPPED entirely and validation is per-element only. Splitting an identifier or keyword across the element boundary defeats every guard because neither half tokenizes as the forbidden thing.

Confirmed live against astra HEAD via `MappingTransformer().render`:
- `['de','v.internal.pii_raw.amount']` -> `dev.internal.pii_raw.amount` (unreviewed SOURCE relation)
- `['de','v.sales.orders.amount']` -> `dev.sales.orders.amount` (reviewed table but left pointing at SOURCE, not prod)
- `['1 UNI','ON SELECT 1']` -> `1 UNION SELECT 1`
- `['1 DR','OP TAB','LE prod.sales.orders']` -> `1 DROP TABLE prod.sales.orders`

Fix-introduced regression, bisected to commit 89a150e ("M07 fix B2"). At base d1163a3 all four raise `Unsupported SQL`. No compensating control: `structured_identifiers()` yields [] for these payloads so the leaked relation never reaches preflight `dependencies.check` or manifest `required_sources`. Same class as B1's silent-source-leak, re-entering via the SQL-text path.

**Required fix:** Always run the statement-wide guard over the concatenated fragments, passing `fragment=fragment_mode` so the SELECT anchor stays relaxed where legitimate:

```python
map_sql(''.join(rendered), mappings, fragment=fragment_mode)
```

This ALONE is insufficient — the per-element results must also be CONSISTENT with the joined parse. Prefer validating the concatenation as the single source of truth and re-splitting at the original element lengths, so a split identifier cannot map differently than the whole. Ensure the concatenated relation identifiers are mapped (not just guarded) so a reviewed source relation is rewritten to target, not left pointing at source.

**Named RED test** (`backend/tests/test_vc_mapping.py`) — verified RED 4/4 on astra HEAD:
```python
@pytest.mark.parametrize('elements', [
    ['de', 'v.internal.pii_raw.amount'],
    ['de', 'v.sales.orders.amount'],
    ['1 UNI', 'ON SELECT 1'],
    ['1 DR', 'OP TAB', 'LE prod.sales.orders'],
])
def test_snippet_sql_arrays_are_validated_after_concatenation(package_rig, elements):
    artifact = {'serialized_space': {'instructions': {'sql_snippets': {
        'measures': [{'sql': list(elements)}]}}}}
    with pytest.raises(ValueError):
        MappingTransformer().render(artifact, package_rig.mapping, package_rig.target)
```

---

## BLOCK G2 — F2 guards only the trailing element; `join_specs.sql[0]` accepts arbitrary statement classes

**Location:** `backend/services/version_control/promotion/mapping.py:142-149`

**Why wrong:** F2 correctly `fullmatch`es the `--rt=` annotation on `index > 0`, but element 0 goes through `map_sql(..., fragment=True)`, which drops the `code[0].upper() != 'SELECT'` anchor (line 63). Since GRANT/CREATE/ALTER/REVOKE are absent from the forbidden-keyword set (line 64-65 lists only IDENTIFIER, EXECUTE, IMMEDIATE, WITH, UNION, PIVOT, LATERAL, TABLE, USE, INSERT, UPDATE, DELETE, DROP), a well-formed 2-element array with a valid annotation smuggles a non-join statement:
- `['GRANT SELECT ON prod.sales.orders TO evil', '--rt=...MANY_TO_ONE--']` -> PASSES
- `['CREATE VIEW leak AS SELECT 1', '--rt=...MANY_TO_ONE--']` -> PASSES
- `` ['`hive`.`internal`.`pii_raw`.`c` = `o`.`c`', '--rt=...--'] `` -> PASSES (foreign-catalog relation, unreviewed)

All three raise at base d1163a3. Same `fragment=True` SELECT-anchor loss also lets single-element snippets carry GRANT/CREATE VIEW/ALTER VIEW.

**Required fix:** Constrain element 0 to the documented join-condition grammar rather than relying on the general SQL guard. Per `backend/references/schema.md:205`, it is a backtick-quoted equality: `` `alias`.`col` = `alias`.`col` ``. Validate that shape POSITIVELY (allowlist), AND extend the forbidden-keyword set with the DDL/DCL verbs (GRANT, REVOKE, CREATE, ALTER, REPLACE, MERGE, COPY) so fragment mode is not a blanket statement-class bypass.

**Named RED test** (`backend/tests/test_vc_mapping.py`) — verified RED 3/3 on astra HEAD:
```python
@pytest.mark.parametrize('condition', [
    'GRANT SELECT ON prod.sales.orders TO evil',
    'CREATE VIEW leak AS SELECT 1',
    '`hive`.`internal`.`pii_raw`.`c` = `o`.`c`',
])
def test_join_condition_element_is_statement_class_guarded(package_rig, condition):
    artifact = {'serialized_space': {'instructions': {'join_specs': [{
        'left': {'identifier': 'dev.sales.orders'}, 'right': {'identifier': 'dev.sales.orders'},
        'sql': [condition, '--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--']}]}}}
    with pytest.raises(ValueError):
        MappingTransformer().render(artifact, package_rig.mapping, package_rig.target)
```

---

## Non-blocking (record, optional)
- join_specs with no `sql` key silently accepted (schema.md:205 makes sql required). Pre-existing, not a leak.
- ReleaseCatalog empty-facts LookupError test-precision: pin exception type explicitly. Minor.

## Success criteria
- Both new RED tests fail on current d1a489e code, pass after fix.
- Full backend offline suite green (was 1182), integration test still @pytest.mark.integration deselected.
- No regression to B1/B3/B4/F1 tests.
- Owned-paths: only mapping.py + test_vc_mapping.py (+ other owned test files if needed).
- MUST run on Astra (pi/system.ai.gpt-6-astra or codex/databricks-gpt-6-astra).
