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
from backend.tests.test_vc_packages import MemoryFiles, package_rig, uid


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
    rig.service.receipt_store = MemoryFiles()
    rig.service.receipt_volume = '/Volumes/control/vc/vc_target_receipts'
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
    rig.post = rig.service.canonicalizer.observe({'serialized_space': vc.to_wire(rig.request.serialized_space),
                                               'description': rig.request.description})
    rig.preimage = vc.ObservationRef(uid(10), rig.target.binding_id, 1, rig.base.state_digest, rig.base.response_envelope_digest)
    rig.postimage = vc.ObservationRef(uid(11), rig.target.binding_id, 1, rig.post.state_digest, rig.post.response_envelope_digest)
    rig.result = vc.OperationResult(uid(4), vc.OperationStatus.CONFIRMED, rig.preimage, rig.postimage, False, ())
    rig.rows = []
    rig.facts.lookup_request.side_effect = lambda binding, key: vc.RequestHistory(tuple(rig.rows), False)
    rig.facts.append.side_effect = lambda fact: rig.rows.append(fact)
    def execute(request, executor):
        terminal = vc.OperationFact(event_id=uid(12), fact_kind=vc.FactKind.OPERATION,
            operation_id=uid(4), transition_sequence=3, event_key='terminal', binding=rig.target,
            request=rig.request.identity, operation_type='promotion', requester_id='requester',
            actor=vc.ActorContext('target-sp', 'target', 'service_principal'), status=vc.FactStatus.CONFIRMED,
            evidence=rig.approval.request.inputs, recorded_at=datetime.now(timezone.utc), attempt_id=uid(13),
            generation=7, approval_id=uid(4), approval_digest=rig.approval.approval_digest,
            pre_version_id=uid(10), post_version_id=uid(11), job_run_id=rig.executor.execution_ref)
        rig.rows.append(terminal)
        return rig.result
    rig.gate.execute.side_effect = execute
    rig.gate.verify_only.return_value = rig.result
    source_version = rig.version
    rig.ledger.get_version.side_effect = lambda binding, version_id: (source_version if binding == rig.source else
        vc.Version(version_id, rig.base if version_id == uid(10) else rig.post,
                   replace(source_version.context, binding=rig.target)))
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


@pytest.mark.parametrize('case', ['missing', 'revision', 'drift', 'busy', 'stale', 'quarantined', 'unknown'])
def test_target_drift_or_missing_pre_enrollment_blocks_without_create(promotion_rig, case):
    rig = promotion_rig
    approve(rig)
    observation = rig.observer.capture.return_value
    if case == 'missing':
        rig.registry.resolve.return_value = None
    elif case == 'revision':
        rig.registry.resolve.return_value = replace(rig.target, binding_revision=2)
    elif case == 'drift':
        observation.captured_version.fingerprints = rig.version.snapshot.fingerprints
    elif case == 'busy':
        observation.busy = True
    elif case == 'stale':
        observation.status.stale = True
    elif case == 'quarantined':
        observation.status.quarantined = True
    else:
        observation.status.drift = vc.DriftState.UNKNOWN
    with pytest.raises(ValueError):
        rig.service.preflight(rig.request.identity.operation_id, rig.executor)
    rig.registry.enroll.assert_not_called()
    rig.gate.create.assert_not_called()
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
        rig.ledger.get_version.side_effect = None
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


def test_receipt_binds_intended_rendered_observed_and_pre_post_versions(promotion_rig):
    rig = promotion_rig
    approve(rig)
    receipt = rig.service.execute(uid(4), rig.executor)
    assert isinstance(receipt, vc.DeploymentReceipt), 'Promotion must return a durable typed deployment receipt'
    assert receipt.source_version_id == rig.version.version_id
    assert receipt.intended_fingerprints == rig.version.snapshot.fingerprints
    assert receipt.rendered_fingerprints == receipt.observed_fingerprints == rig.post.fingerprints
    assert receipt.intended_fingerprints != receipt.rendered_fingerprints
    assert (receipt.pre_version_id, receipt.post_version_id) == (uid(10), uid(11))
    assert (receipt.attempt_id, receipt.generation, receipt.job_run_id) == (uid(13), 7, 'job/123')
    assert receipt.approval_digest == rig.approval.approval_digest
    assert receipt.package_digest == rig.reference.package_digest
    assert receipt.coordination_backend == 'delta'
    assert rig.facts.append.call_args.args[0].evidence == receipt
    assert rig.facts.append.call_args.args[0].fact_kind == vc.FactKind.RECEIPT
    assert rig.tests.run.call_count == 3


def test_receipt_export_failure_retries_export_not_patch(promotion_rig):
    rig = promotion_rig
    approve(rig)
    store = rig.service.receipt_store
    original_put = store.put_if_absent
    store.put_if_absent = Mock(side_effect=OSError('Reverse share unavailable'))
    with pytest.raises(OSError):
        rig.service.execute(uid(4), rig.executor)
    durable = [fact.evidence for fact in rig.rows if fact.fact_kind == vc.FactKind.RECEIPT]
    assert len(durable) == 1
    tests_count = rig.tests.run.call_count
    store.put_if_absent = original_put
    rig.store.read = Mock(side_effect=OSError('Source offline'))
    receipt = rig.service.execute(uid(4), rig.executor)
    assert receipt == durable[0]
    rig.gate.execute.assert_called_once()
    rig.gate.verify_only.assert_not_called()
    assert rig.tests.run.call_count == tests_count
    path = rig.service.receipt_volume + '/sha256/' + digest(receipt) + '/receipt.json'
    assert store.read(path) == encode(receipt)
    assert rig.service.execute(uid(4), rig.executor) == receipt
    assert len([fact for fact in rig.rows if fact.fact_kind == vc.FactKind.RECEIPT]) == 1


@pytest.mark.parametrize('case', ['authorized', 'no_policy', 'drift', 'ambiguous', 'unresolved', 'late_authorization', 'failed_restore'])
def test_failed_smoke_compensation_needs_preauthorization_and_matching_post_stage(promotion_rig, case):
    rig = promotion_rig
    approve(rig)
    compensation = replace(rig.request, identity=vc.RequestIdentity(uid(20), 'compensate-once', 'b' * 64),
        operation_type='restore', source_version_id=uid(10), expected_base=rig.post.state_digest,
        serialized_space=rig.base.serialized_space, description='Old', approval_id=uid(20))
    policy = {} if case == 'no_policy' else {'compensation_operation_id': uid(20),
                                           'compensation_request_digest': compensation.identity.request_digest}
    inputs = replace(rig.approval.request.inputs, recovery_policy=policy)
    requested_at = datetime.now(timezone.utc) + timedelta(hours=1) if case == 'late_authorization' else rig.approval.request.requested_at
    rig.approval = replace(rig.approval, request=replace(rig.approval.request, inputs=inputs, requested_at=requested_at))
    rig.facts.get_request.side_effect = lambda operation_id: vc.ApprovedOperation(
        rig.request if operation_id == uid(4) else compensation, rig.approval)
    rig.service.restore = Mock()
    rig.service.restore.compensate.return_value = replace(rig.result, operation_id=uid(20),
        status=vc.OperationStatus.APPLIED_UNVERIFIED if case == 'failed_restore' else vc.OperationStatus.CONFIRMED)
    original_observation = rig.observer.capture.return_value
    def observe(binding, reason, executor):
        if reason == 'promotion_compensation':
            return Mock(spec=vc.ObservationResult, busy=False,
                captured_version=Mock(spec=vc.VersionSummary, fingerprints=rig.base.fingerprints if case == 'drift' else rig.post.fingerprints),
                status=Mock(spec=vc.BindingStatus, stale=False, quarantined=False))
        return original_observation
    rig.observer.capture.side_effect = observe
    rig.tests.run.side_effect = [
        {'validation': {'passed': True}, 'benchmark': {'passed': True}},
        {'validation': {'passed': False}, 'benchmark': {'passed': True}}]
    if case == 'ambiguous':
        rig.facts.lookup_request.side_effect = lambda binding, key: vc.RequestHistory(tuple(rig.rows), bool(rig.rows))
        with pytest.raises(ValueError):
            rig.service.execute(uid(4), rig.executor)
        rig.service.restore.compensate.assert_not_called()
        return
    if case == 'unresolved':
        rig.result = replace(rig.result, status=vc.OperationStatus.APPLIED_UNVERIFIED, unresolved=True)
    receipt = rig.service.execute(uid(4), rig.executor)
    if case in {'authorized', 'failed_restore'}:
        rig.service.restore.compensate.assert_called_once_with(uid(4), uid(20), rig.executor)
        assert receipt.compensation_operation_id == uid(20)
        assert receipt.status == (vc.OperationStatus.COMPENSATED if case == 'authorized' else vc.OperationStatus.FAILED)
    else:
        rig.service.restore.compensate.assert_not_called()
        assert receipt.status != vc.OperationStatus.COMPENSATED
