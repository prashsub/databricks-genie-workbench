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
