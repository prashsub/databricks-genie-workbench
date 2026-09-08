"""Portable-to-target mapping golden cases."""

from copy import deepcopy
from dataclasses import replace

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.promotion.mapping import MappingTransformer
from backend.tests.test_vc_packages import package_rig


def test_structured_tables_metric_views_catalogs_schemas_map_exactly(package_rig):
    rig = package_rig
    mappings = {'dev.sales.orders': 'prod.reporting.orders', 'dev.sales.metrics': 'prod.reporting.metrics',
                'dev': 'prod', 'dev.sales': 'prod.reporting'}
    mapping = replace(rig.mapping, mappings=mappings)
    artifact = {'serialized_space': {'data_sources': {
        'tables': [{'identifier': 'dev.sales.orders', 'description': 'dev.sales.orders'}],
        'metric_views': [{'identifier': 'dev.sales.metrics'}],
        'catalogs': [{'identifier': 'dev'}], 'schemas': [{'identifier': 'dev.sales'}]}}, 'description': 'dev.sales'}
    original = deepcopy(artifact)
    rendered = MappingTransformer().render(artifact, mapping, rig.target)
    sources = rendered.serialized_space['data_sources']
    assert [sources[kind][0]['identifier'] for kind in ('tables', 'metric_views', 'catalogs', 'schemas')] == [
        'prod.reporting.orders', 'prod.reporting.metrics', 'prod', 'prod.reporting']
    assert sources['tables'][0]['description'] == 'dev.sales.orders'
    assert rendered.description == 'dev.sales'
    assert artifact == original


def test_join_specs_and_sql_function_identifiers_map_exactly_or_fail_closed(package_rig):
    mappings = {
        **dict(package_rig.mapping.mappings),
        'dev.sales.customers': 'prod.sales.customers',
        'dev.sales.fiscal_quarter': 'prod.sales.fiscal_quarter',
    }
    mapping = replace(package_rig.mapping, mappings=mappings)
    artifact = {'serialized_space': {'instructions': {
        'join_specs': [{
            'left': {'identifier': 'dev.sales.orders'},
            'right': {'identifier': 'dev.sales.customers'},
        }],
        'sql_functions': [{'identifier': 'dev.sales.fiscal_quarter'}],
    }}}

    rendered = MappingTransformer().render(artifact, mapping, package_rig.target).serialized_space

    assert rendered['instructions']['join_specs'][0]['left']['identifier'] == 'prod.sales.orders'
    assert rendered['instructions']['join_specs'][0]['right']['identifier'] == 'prod.sales.customers'
    assert rendered['instructions']['sql_functions'][0]['identifier'] == 'prod.sales.fiscal_quarter'

    artifact['serialized_space']['instructions']['future_field'] = [{'identifier': 'dev.sales.orders'}]
    with pytest.raises(ValueError, match='identifier'):
        MappingTransformer().render(artifact, mapping, package_rig.target)


def test_sql_identifier_mapping_preserves_literals_comments_and_instruction_prose(package_rig):
    rig = package_rig
    sql = "SELECT 'dev.sales.orders', total FROM `dev`.`sales`.`orders` -- dev.sales.orders\n/* dev.sales.orders */"
    artifact = {'serialized_space': {'instructions': {
        'example_question_sqls': [{'sql': [sql]}],
        'text_instructions': [{'content': ['Use dev.sales.orders as an example.']}]},
        'benchmarks': {'questions': [{'answer': [{'format': 'SQL', 'content': ['SELECT * FROM dev.sales.orders']}]}]}}}
    result = MappingTransformer().render(artifact, rig.mapping, rig.target).serialized_space
    assert result['instructions']['example_question_sqls'][0]['sql'] == [sql.replace('`dev`.`sales`.`orders`', '`prod`.`sales`.`orders`')]
    assert result['benchmarks']['questions'][0]['answer'][0]['content'] == ['SELECT * FROM prod.sales.orders']
    assert result['instructions']['text_instructions'] == artifact['serialized_space']['instructions']['text_instructions']


@pytest.mark.parametrize('sql', [
    "SELECT * FROM IDENTIFIER('dev.sales.orders')", 'EXECUTE IMMEDIATE sql_text',
    'SELECT * FROM dev.sales.unmapped', 'SELECT * FROM orders',
    'SELECT * FROM dev . sales . orders', 'SELECT * FROM `dev.sales.orders`',
    'SELECT * FROM dev.sales.orders; DROP TABLE prod.sales.orders',
    "SELECT 'unterminated FROM dev.sales.orders", 'SELECT * FROM dev.sales.orders /* unclosed',
    'SELECT * FROM dev.sales.orders_suffix', 'SELECT * FROM sales.orders',
])
def test_unsupported_sql_or_unresolved_source_identifier_fails_closed(package_rig, sql):
    artifact = {'serialized_space': {'instructions': {'example_question_sqls': [{'sql': [sql]}]}}}
    with pytest.raises(ValueError):
        MappingTransformer().render(artifact, package_rig.mapping, package_rig.target)


@pytest.mark.parametrize('field', ['expression', 'sql_expression', 'query', 'function_name'])
def test_unknown_executable_fields_require_review(package_rig, field):
    artifact = {'serialized_space': {'instructions': {'sql_snippets': [{field: 'dev.sales.unmapped'}]}}}
    with pytest.raises(ValueError):
        MappingTransformer().render(artifact, package_rig.mapping, package_rig.target)


def test_reviewed_exact_sql_override_is_bound_in_mapping(package_rig):
    sql = "SELECT * FROM IDENTIFIER('dev.sales.orders')"
    mapping = replace(package_rig.mapping, mappings={**dict(package_rig.mapping.mappings),
        'sql:' + sql: 'SELECT * FROM prod.sales.orders'})
    artifact = {'serialized_space': {'instructions': {'example_question_sqls': [{'sql': [sql]}]}}}
    rendered = MappingTransformer().render(artifact, mapping, package_rig.target)
    assert rendered.serialized_space['instructions']['example_question_sqls'][0]['sql'] == ['SELECT * FROM prod.sales.orders']


def test_join_spec_and_snippet_sql_fragments_map_identifiers_and_preserve_arity(package_rig):
    join_condition = '`dev`.`sales`.`orders`.`customer_id` = `dev`.`sales`.`customers`.`customer_id`'
    relationship = '--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--'
    snippet = "IDENTIFIER('dev.sales.orders').amount"
    reviewed_snippet = 'prod.sales.orders.amount'
    mapping = replace(package_rig.mapping, mappings={
        **dict(package_rig.mapping.mappings),
        'dev.sales.customers': 'prod.sales.customers',
        'fragment:' + snippet: reviewed_snippet,
    })
    artifact = {'serialized_space': {'instructions': {
        'join_specs': [{'left': {'identifier': 'dev.sales.orders'},
                        'right': {'identifier': 'dev.sales.customers'},
                        'sql': [join_condition, relationship]}],
        'sql_snippets': {'measures': [{'sql': [snippet]}]},
    }}}

    rendered = MappingTransformer().render(artifact, mapping, package_rig.target).serialized_space

    sql = rendered['instructions']['join_specs'][0]['sql']
    assert sql[0] == '`prod`.`sales`.`orders`.`customer_id` = `prod`.`sales`.`customers`.`customer_id`'
    assert len(sql) == 2
    assert sql[1] == relationship
    assert rendered['instructions']['sql_snippets']['measures'][0]['sql'] == [reviewed_snippet]


@pytest.mark.parametrize('trailing', [
    ['SELECT * FROM dev.internal.pii_raw'],
    ['DROP TABLE prod.sales.orders'],
    ['--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--\nUNION SELECT 1'],
    ['--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--', 'SELECT * FROM dev.internal.pii_raw'],
])
def test_join_spec_sql_trailing_elements_are_validated_not_passthrough(package_rig, trailing):
    # F2: join_specs.sql elements after index 0 must NOT be verbatim passthroughs.
    # The trailing element(s) must be exactly the reviewed --rt-- annotation and there
    # must be no element index > 1; anything else fails closed.
    join_condition = '`dev`.`sales`.`orders`.`customer_id` = `dev`.`sales`.`customers`.`customer_id`'
    mapping = replace(package_rig.mapping, mappings={
        **dict(package_rig.mapping.mappings),
        'dev.sales.customers': 'prod.sales.customers',
    })
    artifact = {'serialized_space': {'instructions': {
        'join_specs': [{'left': {'identifier': 'dev.sales.orders'},
                        'right': {'identifier': 'dev.sales.customers'},
                        'sql': [join_condition, *trailing]}]}}}
    with pytest.raises(ValueError):
        MappingTransformer().render(artifact, mapping, package_rig.target)


def test_sql_prefix_mapping_requires_exact_or_three_part_source(package_rig):
    # F1: a one-token catalog mapping {'dev':'prod'} must NOT authorize an unbounded
    # subtree via longest-prefix matching. 'dev.internal.pii_raw' was never reviewed;
    # promoting it to 'prod.internal.pii_raw' leaks a never-reviewed source table.
    mapping = replace(package_rig.mapping, mappings={'dev': 'prod'})
    artifact = {'serialized_space': {'instructions': {
        'example_question_sqls': [{'sql': ['SELECT * FROM dev.internal.pii_raw']}]}}}
    with pytest.raises(ValueError):
        MappingTransformer().render(artifact, mapping, package_rig.target)


@pytest.mark.parametrize('case', ['target', 'wrong_binding', 'injected_environment'])
def test_warehouse_folder_binding_and_principals_remain_target_local(package_rig, case):
    rig = package_rig
    mapping = replace(rig.mapping, mappings={**dict(rig.mapping.mappings),
        'target:warehouse_id': 'target-warehouse', 'target:parent_path': '/target/folder',
        'target:consumer:analysts': 'target-analysts'})
    artifact = {'serialized_space': {'data_sources': {'tables': [{'identifier': 'dev.sales.orders'}]}}}
    target = replace(rig.target, binding_revision=2) if case == 'wrong_binding' else rig.target
    if case == 'injected_environment':
        artifact['serialized_space']['config'] = {'warehouse_id': 'source-warehouse'}
    if case != 'target':
        with pytest.raises(ValueError):
            MappingTransformer().render(artifact, mapping, target)
        return
    result = MappingTransformer().render(artifact, mapping, target)
    assert getattr(result, 'environment', None) == {'warehouse_id': 'target-warehouse',
        'parent_path': '/target/folder', 'consumers': ('target-analysts',)}
    assert 'warehouse_id' not in result.serialized_space
    assert 'permissions' not in result.serialized_space
