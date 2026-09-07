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
