"""Target-local promotion safety contract tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.promotion.mapping import MappingTransformer
from backend.services.version_control.promotion.packages import digest, encode
from backend.services.version_control.governance.approvals import request_digest, approval_digest
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
        {'version': 2, 'data_sources': {'tables': [{'identifier': 'prod.sales.orders'}]}}, 'Sales', uid(4))
    rig.request = replace(rig.request, identity=replace(rig.request.identity, request_digest=request_digest(rig.request)))
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
    rig.identity.executor.return_value = rig.executor
    rig.gate = Mock(spec=vc.MutationGate)
    rig.registry = Mock(spec=vc.Registry)
    rig.registry.resolve.return_value = rig.target
    rig.observer = Mock(spec=vc.Observer)
    rig.observer.capture.return_value = Mock(spec=vc.ObservationResult, busy=False,
        captured_version=Mock(spec=vc.VersionSummary, fingerprints=rig.base.fingerprints),
        status=Mock(spec=vc.BindingStatus, stale=False, quarantined=False, drift=vc.DriftState.CLEAN))
    for name in ('facts', 'releases', 'dependencies', 'tests', 'identity', 'gate', 'registry', 'observer'):
        setattr(rig.service, name, getattr(rig, name))
    rig.service.transformer = MappingTransformer()
    rig.service.workspace_id = 'target'
    rig.service.target_host = rig.executor.host
    rig.service.target_selection = vc.ExplicitExecutorSelection('target', rig.executor.host, 'target-sp',
                                                               rig.executor.execution_ref, 'target-profile')
    return rig


def approve(rig):
    evidence = rig.service.preflight(rig.request.identity.operation_id, rig.executor)
    manifest = json.loads(rig.store.read(rig.reference.manifest_uri))
    rendered = vc.canonical_json_hash('vc-rendered-target/1', {
        'serialized_space': rig.request.serialized_space, 'description': rig.request.description})
    inputs = vc.ApprovalInputs('VC/1.0', rig.request.identity.operation_id, 'promotion', rig.version.version_id,
        rig.version.snapshot.raw_state_digest, rig.version.snapshot.fingerprints,
        manifest['artifact_digest'], manifest['mapping_digest'], rendered, 'vc-map/1', 'vc-c14n/1',
        rig.target, rig.base.fingerprints, evidence.permission_policy_digest,
        manifest['validation_policy_digest'], manifest['benchmark_policy_digest'], evidence.preflight_evidence_digest,
        rig.policy.thresholds, 'requester', {}, datetime.now(timezone.utc) + timedelta(hours=1))
    votes = (vc.ApprovalVote('target-human-1', 'approve', datetime.now(timezone.utc)),
             vc.ApprovalVote('target-human-2', 'approve', datetime.now(timezone.utc)))
    rig.approval = vc.ApprovalRecord(vc.ApprovalRequest(rig.request.approval_id, inputs, datetime.now(timezone.utc)),
        votes, vc.FactStatus.APPROVED, approval_digest(inputs, votes))
    rig.facts.get_request.return_value = vc.ApprovedOperation(rig.request, rig.approval)
    rig.approvals = Mock(spec=vc.ApprovalService)
    rig.service.approvals = rig.approvals
    return evidence


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


@pytest.mark.parametrize('changed', [None, 'artifact', 'mapping', 'validation', 'manifest', 'render', 'policy', 'source'])
def test_executor_recomputes_artifact_mapping_render_and_policy_digests(promotion_rig, changed):
    rig = promotion_rig
    approve(rig)
    if changed in {'artifact', 'mapping', 'validation', 'manifest'}:
        path = rig.reference.manifest_uri.replace('manifest.json', changed + '.json')
        value = json.loads(rig.store.read(path))
        if changed == 'artifact':
            value['description'] = 'tampered'
        elif changed == 'mapping':
            value['mappings']['dev.sales.orders'] = 'attacker.sales.orders'
        elif changed == 'validation':
            value['thresholds']['benchmark']['minimum'] = 0
        else:
            value['raw_source_digest'] = 'f' * 64
        rig.store.files[path] = encode(value)
    elif changed == 'render':
        rig.facts.get_request.return_value = vc.ApprovedOperation(replace(rig.request, description='tampered'), rig.approval)
    elif changed == 'policy':
        inputs = replace(rig.approval.request.inputs, validation_policy_digest='e' * 64)
        rig.facts.get_request.return_value = vc.ApprovedOperation(rig.request,
            replace(rig.approval, request=replace(rig.approval.request, inputs=inputs)))
    elif changed == 'source':
        rig.ledger.get_version.return_value = replace(rig.version, snapshot=rig.base)
    if changed:
        with pytest.raises(ValueError):
            rig.service.execute(rig.request.identity.operation_id, rig.executor)
        rig.gate.execute.assert_not_called()
    else:
        rig.service.execute(rig.request.identity.operation_id, rig.executor)
        rig.gate.execute.assert_called_once_with(rig.request, rig.executor)


@pytest.mark.parametrize('case', ['source_only', 'wrong_host', 'wrong_workspace', 'wrong_run_as', 'default_profile', 'disabled'])
def test_source_only_approval_or_wrong_target_host_cannot_deploy(promotion_rig, case):
    rig = promotion_rig
    approve(rig)
    executor = rig.executor
    if case == 'source_only':
        rig.approvals.authorize.side_effect = PermissionError('No target approver remains eligible')
    elif case == 'wrong_host':
        executor = replace(executor, host='https://source.example.com')
    elif case == 'wrong_workspace':
        executor = replace(executor, workspace_id='source')
    elif case == 'wrong_run_as':
        rig.identity.verify_run_as.side_effect = PermissionError('Wrong run_as')
    elif case == 'default_profile':
        rig.service.target_selection = replace(rig.service.target_selection, profile='DEFAULT')
    elif case == 'disabled':
        from backend.services.version_control.platform.feature_flags import FeatureFlags
        rig.service.flags = FeatureFlags()
    with pytest.raises(PermissionError):
        rig.service.execute(rig.request.identity.operation_id, executor)
    rig.gate.execute.assert_not_called()
