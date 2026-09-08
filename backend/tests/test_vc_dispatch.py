"""Target operation polling and native Job retry tests."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts as vc
from backend.tests.test_vc_packages import package_rig, uid
from backend.tests.test_vc_promotion import promotion_rig, approve


def test_polling_discovers_only_local_approved_pending_operations(promotion_rig):
    rig = promotion_rig
    approve(rig)
    local = vc.ApprovedOperation(rig.request, rig.approval)
    wrong = vc.ApprovedOperation(replace(rig.request, binding=rig.source), rig.approval)
    unapproved = vc.ApprovedOperation(replace(rig.request, identity=replace(rig.request.identity,
        operation_id=uid(8))), None)
    completed = replace(rig.request, identity=replace(rig.request.identity, operation_id=uid(9)))
    operations = {uid(4): local, uid(7): wrong, uid(8): unapproved,
                  uid(9): vc.ApprovedOperation(completed, rig.approval)}
    rig.facts.get_request.side_effect = operations.__getitem__
    rig.service.pending = Mock()
    rig.service.pending.scan.return_value = SimpleNamespace(items=(
        vc.OperationHandle(uid(4), vc.OperationStatus.REQUESTED, None),
        vc.OperationHandle(uid(7), vc.OperationStatus.REQUESTED, None),
        vc.OperationHandle(uid(8), vc.OperationStatus.REQUESTED, None),
        vc.OperationHandle(uid(9), vc.OperationStatus.CONFIRMED, 'job/old')), next_cursor='next')
    rig.service.dispatcher = Mock(spec=vc.JobDispatcher)
    rig.service.dispatcher.submit_local.return_value = vc.OperationHandle(uid(4), vc.OperationStatus.REQUESTED, 'job/new')
    page = rig.service.dispatch_pending('target', 'cursor')
    assert [item.operation_id for item in page.items] == [uid(4)]
    assert page.next_cursor == 'next'
    rig.service.pending.scan.assert_called_once_with('target', 'cursor')
    rig.service.dispatcher.submit_local.assert_called_once_with(uid(4), 'promotion')
    rig.gate.execute.assert_not_called()
    with pytest.raises(PermissionError):
        rig.service.dispatch_pending('source', None)


@pytest.mark.parametrize('retry', ['completed', 'unresolved', 'changed_digest', 'ambiguous'])
def test_duplicate_dispatch_or_job_retry_does_not_replay_mutation(promotion_rig, retry):
    rig = promotion_rig
    approve(rig)
    rig.service.execute(uid(4), rig.executor)
    fact = Mock(spec=vc.OperationFact, request=rig.request.identity, fact_kind=vc.FactKind.OPERATION,
                status=vc.FactStatus.CONFIRMED, operation_id=uid(4))
    if retry == 'unresolved':
        fact.status = vc.FactStatus.APPLIED_UNVERIFIED
    if retry == 'changed_digest':
        fact.request = replace(rig.request.identity, request_digest='e' * 64)
    rig.facts.lookup_request.side_effect = None
    rig.facts.lookup_request.return_value = vc.RequestHistory((fact,), retry == 'ambiguous')
    if retry in {'changed_digest', 'ambiguous'}:
        with pytest.raises(ValueError):
            rig.service.execute(uid(4), rig.executor)
    else:
        rig.service.execute(uid(4), rig.executor)
        rig.gate.verify_only.assert_called_once_with(uid(4), rig.executor)
    rig.gate.execute.assert_called_once()


def test_native_promotion_job_accepts_operation_id_only(promotion_rig):
    from backend.jobs import vc_promote
    rig = promotion_rig
    service = Mock()
    vc_promote.run(uid(4), service=service, executor=rig.executor)
    service.execute.assert_called_once_with(uid(4), rig.executor)
    with pytest.raises(ValueError):
        vc_promote.run('not-an-operation-id', service=service, executor=rig.executor)


def test_release_routes_authenticate_require_keys_and_return_pending(promotion_rig):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.routers.vc_releases import build_router
    rig = promotion_rig
    commands = Mock()
    commands.promote.return_value = vc.OperationHandle(uid(4), vc.OperationStatus.REQUESTED, None)
    commands.receipt.return_value = None
    app = FastAPI()
    app.include_router(build_router(commands=commands, identity=rig.identity))
    authentication_enabled = False
    @app.middleware('http')
    async def authenticate(request, call_next):
        if authentication_enabled:
            request.state.vc_auth = vc.AuthenticatedRequest('server-verified', 'target')
        return await call_next(request)
    with TestClient(app) as client:
        response = client.get(f'/api/version-control/releases/{uid(4)}/receipt')
        assert response.status_code == 401
    authentication_enabled = True
    rig.identity.actor.return_value = vc.ActorContext('human', 'target', 'human')
    with TestClient(app) as client:
        path = f'/api/version-control/releases/{uid(4)}/promote'
        assert client.post(path, json={'approval_id': uid(4)}).status_code == 422
        response = client.post(path, json={'approval_id': uid(4)}, headers={'Idempotency-Key': 'promote-once'})
        assert response.status_code == 202
        commands.promote.assert_called_once_with(uid(4), uid(4), 'promote-once', rig.identity.actor.return_value)
        assert client.get(f'/api/version-control/releases/{uid(4)}/receipt').status_code == 202
        commands.promote.side_effect = PermissionError('Target scope denied')
        assert client.post(path, json={'approval_id': uid(4)}, headers={'Idempotency-Key': 'key'}).status_code == 403
        assert client.post(path, json={'approval_id': uid(4), 'token': 'not-accepted'},
                           headers={'Idempotency-Key': 'key'}).status_code == 422


def test_promotion_ddl_owns_only_two_distinct_volumes():
    from pathlib import Path
    path = Path(__file__).parents[1] / 'version_control_ddl/08-promotion-volumes.sql'
    assert path.exists(), 'M07 separate Volume provisioning manifest is missing'
    ddl = path.read_text()
    assert ddl.count('CREATE VOLUME IF NOT EXISTS') == 2
    assert '${catalog}.${control_schema}.vc_outbound_packages' in ddl
    assert '${catalog}.${control_schema}.vc_target_receipts' in ddl
    assert 'CREATE TABLE' not in ddl and 'vc_approval_evidence' not in ddl


def test_release_registration_is_not_an_executable_command(promotion_rig):
    from backend.services.version_control.promotion.releases import ReleaseCommands
    rig = promotion_rig
    written = []
    commands = ReleaseCommands(promotion=rig.service,
        request_writer=lambda request, fact: written.append((request, fact)), authorize_history=lambda actor, binding: True)
    actor = vc.ActorContext('target-human', 'target', 'human')
    handle = commands.create(rig.version.version_id, rig.mapping, rig.policy, rig.base.state_digest,
                             'source-profile', 'target-profile', 'release-key', actor)
    request, fact = written[0]
    assert fact.fact_kind == vc.FactKind.RELEASE
    assert request.binding == rig.target and request.operation_type == 'promotion'
    assert handle.operation_id == request.identity.operation_id == fact.release_id
    assert request.approval_id == request.identity.operation_id
    assert fact.evidence.source_version_id == rig.version.version_id
    rig.gate.execute.assert_not_called()
    rig.registry.enroll.assert_not_called()
    with pytest.raises(PermissionError):
        commands.create(rig.version.version_id, rig.mapping, rig.policy, rig.base.state_digest,
                        'source-profile', 'DEFAULT', 'other', actor)


def test_release_validation_and_promotion_require_target_approval(promotion_rig):
    from backend.services.version_control.promotion.releases import ReleaseCommands
    rig = promotion_rig
    approve(rig)
    commands = ReleaseCommands(promotion=rig.service, request_writer=Mock(), authorize_history=lambda actor, binding: True)
    actor = vc.ActorContext('target-human', 'target', 'human')
    handle = commands.validate(uid(4), 'promote-once', actor)
    assert handle.operation_id == uid(4)
    fact = rig.facts.append.call_args.args[0]
    assert isinstance(fact.evidence, vc.StageEvidence)
    assert fact.fact_kind == vc.FactKind.OPERATION and fact.status == vc.FactStatus.REQUESTED
    result = commands.promote(uid(4), uid(4), 'promote-once', actor)
    assert result.status == vc.OperationStatus.REQUESTED
    rig.approvals.authorize.assert_called_once_with(rig.request, rig.executor)
    assert commands.receipt(uid(4), actor) is None
    rig.gate.execute.assert_not_called()
    rig.facts.get_request.return_value = vc.ApprovedOperation(rig.request, None)
    with pytest.raises(PermissionError):
        commands.promote(uid(4), uid(4), 'promote-once', actor)


def test_pending_projection_admits_only_admission_referenced_local_promotion_facts(promotion_rig):
    from backend.services.version_control.promotion.dispatch import PendingOperations
    from backend.services.version_control.promotion.releases import ADMISSION_REFERENCE, ReleaseCommands
    rig = promotion_rig
    approve(rig)
    commands = ReleaseCommands(promotion=rig.service, request_writer=Mock(),
                               authorize_history=lambda actor, binding: True)
    actor = vc.ActorContext('target-human', 'target', 'human')
    commands.validate(uid(4), 'promote-once', actor)
    validated = rig.rows[-1]
    commands.promote(uid(4), uid(4), 'promote-once', actor)
    admitted = rig.rows[-1]
    assert validated.approval_reference is None
    assert admitted.approval_reference == ADMISSION_REFERENCE
    assert validated.fact_kind == admitted.fact_kind == vc.FactKind.OPERATION
    assert validated.status == admitted.status == vc.FactStatus.REQUESTED
    facts = (admitted, validated, replace(admitted, binding=rig.source),
             replace(admitted, operation_type='restore'),
             replace(admitted, fact_kind=vc.FactKind.RELEASE),
             replace(admitted, status=vc.FactStatus.CONFIRMED))
    calls = []
    def read_page(workspace_id, cursor):
        calls.append((workspace_id, cursor))
        return SimpleNamespace(items=facts, next_cursor='next-page')
    page = PendingOperations(read_page).scan('target', 'current-page')
    assert page.items == (vc.OperationHandle(uid(4), vc.OperationStatus.REQUESTED, None),)
    assert page.next_cursor == 'next-page'
    assert calls == [('target', 'current-page')]


@pytest.mark.parametrize('case', ['conflict', 'duplicate', 'wrong_operation', 'wrong_kind', 'wrong_evidence'])
def test_release_catalog_rejects_conflicting_release_facts(promotion_rig, case):
    from backend.services.version_control.promotion.releases import ReleaseCatalog, fact_for
    rig = promotion_rig
    manifest = vc.from_wire(vc.PackageManifest, json.loads(rig.store.read(rig.reference.manifest_uri)))
    fact = fact_for(rig.request, vc.ActorContext('target-human', 'target', 'human'), vc.FactKind.RELEASE, manifest)
    facts = [fact, fact]
    if case == 'conflict':
        facts[1] = replace(fact, evidence=replace(manifest, package_digest='f' * 64))
    elif case == 'wrong_operation':
        facts = [replace(fact, operation_id=uid(7))]
    elif case == 'wrong_kind':
        facts = [replace(fact, fact_kind=vc.FactKind.OPERATION)]
    elif case == 'wrong_evidence':
        facts = [replace(fact, evidence=vc.StageEvidence('VC/1.0', vc.PatchStage.CONFIG_PENDING,
                         fact.recorded_at, 'a' * 64, None))]
    calls = []
    def read_release_facts(operation_id):
        calls.append(operation_id)
        return facts
    catalog = ReleaseCatalog(read_release_facts, rig.service.outbound_volume, rig.executor.host)
    if case == 'duplicate':
        result = catalog.get(uid(4))
        assert result.release_id == uid(4)
        assert result.package == rig.reference
        assert result.target_binding == rig.target
        assert result.target_host == rig.executor.host
    else:
        with pytest.raises(ValueError):
            catalog.get(uid(4))
    assert calls == [uid(4)]


@pytest.mark.parametrize('facts', [[], ()])
def test_release_catalog_empty_facts_raise_exact_lookup_error(facts):
    from backend.services.version_control.promotion.releases import ReleaseCatalog
    read_release_facts = Mock(return_value=facts)
    catalog = ReleaseCatalog(read_release_facts, '/Volumes/catalog/control/outbound', 'target-host')
    with pytest.raises(LookupError, match='^Target-local release not registered$') as error:
        catalog.get(uid(4))
    assert error.type is LookupError  # IndexError is a subclass, but not the catalog contract.
    read_release_facts.assert_called_once_with(uid(4))
