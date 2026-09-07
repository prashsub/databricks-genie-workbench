"""Target-local promotion safety contract tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.promotion.mapping import MappingTransformer
from backend.tests.test_vc_packages import package_rig, uid


@pytest.fixture
def promotion_rig(package_rig):
    rig = package_rig
    rig.mapping = replace(rig.mapping, mappings={**dict(rig.mapping.mappings),
        'target:warehouse_id': 'target-warehouse', 'target:parent_path': '/target/folder',
        'target:consumer:analysts': 'target-analysts'})
    rig.reference = rig.service.package(rig.version.version_id, rig.mapping, rig.policy)
    rig.executor = vc.ExecutorContext('target', 'https://target.example.com', 'target-sp',
                                      'service_principal', object(), 'job/123')
    rig.base = rig.service.canonicalizer.observe({'serialized_space': {'version': 2, 'data_sources': {}}, 'description': 'Old'})
    rig.request = vc.MutationRequest(vc.RequestIdentity(uid(4), 'promote-once', '1' * 64), rig.target,
        'promotion', rig.version.version_id, rig.base.state_digest,
        {'version': 2, 'data_sources': {'tables': [{'identifier': 'prod.sales.orders'}]}}, 'Sales', uid(5))
    rig.facts = Mock(spec=vc.OperationFacts)
    rig.facts.get_request.return_value = vc.ApprovedOperation(rig.request, None)
    rig.facts.lookup_request.return_value = vc.RequestHistory((), False)
    rig.releases = Mock()
    rig.releases.get.return_value = SimpleNamespace(release_id=uid(6), package=rig.reference,
        target_binding=rig.target, target_host=rig.executor.host)
    rig.dependencies = Mock()
    rig.dependencies.check.return_value = True
    rig.tests = Mock()
    rig.tests.run.return_value = {'validation': {'passed': True}, 'benchmark': {'passed': True}}
    rig.identity = Mock(spec=vc.IdentityProvider)
    rig.identity.can_edit.return_value = True
    rig.gate = Mock(spec=vc.MutationGate)
    rig.registry = Mock(spec=vc.Registry)
    rig.registry.resolve.return_value = rig.target
    rig.observer = Mock(spec=vc.Observer)
    rig.observer.capture.return_value = SimpleNamespace(snapshot=rig.base, stale=False, busy=False)
    for name in ('facts', 'releases', 'dependencies', 'tests', 'identity', 'gate', 'registry', 'observer'):
        setattr(rig.service, name, getattr(rig, name))
    rig.service.transformer = MappingTransformer()
    rig.service.workspace_id = 'target'
    rig.service.target_host = rig.executor.host
    return rig


@pytest.mark.parametrize('denied', [None, 'executor_table', 'consumer_table', 'consumer_warehouse', 'executor_folder'])
def test_preflight_checks_executor_and_consumer_dependencies_and_permissions(promotion_rig, denied):
    rig = promotion_rig
    def check(binding, executor, principal, kind, identifier):
        return denied != f'{"executor" if principal == executor.principal_id else "consumer"}_{kind}'
    rig.dependencies.check.side_effect = check
    if denied:
        with pytest.raises(PermissionError):
            rig.service.preflight(rig.request.identity.operation_id, rig.executor)
    else:
        evidence = rig.service.preflight(rig.request.identity.operation_id, rig.executor)
        assert isinstance(evidence, vc.PreflightEvidence)
        checked = {(call.args[2], call.args[3], call.args[4]) for call in rig.dependencies.check.call_args_list}
        for principal in ('target-sp', 'target-analysts'):
            assert {(principal, 'table', 'prod.sales.orders'), (principal, 'warehouse', 'target-warehouse'),
                    (principal, 'folder', '/target/folder')} <= checked
        assert len(evidence.preflight_evidence_digest) == 64
        assert evidence.rendered_fingerprints != rig.version.snapshot.fingerprints
    rig.gate.execute.assert_not_called()
