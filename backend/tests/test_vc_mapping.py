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
