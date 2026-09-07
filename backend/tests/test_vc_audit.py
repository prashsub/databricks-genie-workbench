from dataclasses import replace

import pytest

from backend.services.version_control.contracts import ActorContext, AuthenticatedRequest, BreakGlassRequest, to_wire
from backend.tests.test_vc_approvals import approve, setup_approval
from backend.tests.test_vc_operation_facts import NOW, uid


@pytest.mark.parametrize('availability', ['available', 'lagging', 'unavailable'])
async def test_audit_correlation_is_optional_evidence_not_approval_or_lock(availability):
    from backend.services.version_control.governance.audit import AuditService

    context = setup_approval()
    approve(context)
    before = tuple(context.store.state.rows)
    calls = []

    async def fetch(binding, operation_id, start, end):
        calls.append((binding, operation_id, start, end))
        if availability == 'unavailable':
            raise OSError('audit table unavailable')
        if availability == 'lagging':
            return []
        return [
            {'event_id': 'matching', 'workspace_id': '123', 'space_id': binding.space_id,
             'operation_id': operation_id, 'recorded_at': to_wire(NOW), 'actor_id': None},
            {'event_id': 'foreign', 'workspace_id': 'other', 'space_id': binding.space_id,
             'operation_id': operation_id, 'recorded_at': to_wire(NOW), 'actor_id': 'outsider'},
        ]

    audit = AuditService(context.facts, context.identity, context.clock.now, fetch)
    result = await audit.correlate(uid(2), context.identity.actors['requester'])
    assert calls[0][0] == context.request.binding
    assert result['authoritative'] is False
    assert result['actor'] == 'unknown'
    assert result['availability'] == availability
    assert [event['event_id'] for event in result['events']] == (['matching'] if availability == 'available' else [])
    assert tuple(context.store.state.rows) == before
    assert context.service.authorize(context.request, context.executor).approval_id == uid(2)
    with pytest.raises(PermissionError):
        await audit.correlate(uid(2), ActorContext('outsider', 'other', 'human'))


def test_scoped_operation_and_break_glass_routes_never_serialize_grants():
    from datetime import timedelta
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.routers.vc_approvals import build_router as approvals_router
    from backend.routers.vc_operations import build_router as operations_router
    from backend.services.version_control.governance.audit import AuditService

    context = setup_approval()
    approve(context)
    context.identity.memberships[('requester', '123')] = frozenset({'break-glass'})
    authentication = [AuthenticatedRequest('requester', '123')]
    app = FastAPI()

    @app.middleware('http')
    async def authenticate(request, call_next):
        if authentication[0] is not None:
            request.state.vc_auth = authentication[0]
        return await call_next(request)

    async def fetch(*args):
        return []

    audit = AuditService(context.facts, context.identity, context.clock.now, fetch)
    app.include_router(approvals_router(context.service, context.identity))
    app.include_router(operations_router(audit, context.identity))
    client = TestClient(app)
    response = client.get(f'/api/vc/operations/{uid(2)}')
    assert response.status_code == 200
    assert response.json()['status'] == 'requested'
    override = BreakGlassRequest(context.request.identity, context.request.binding, 'incident',
                                 NOW + timedelta(minutes=15), context.bound.preflight_evidence_digest, True)
    response = client.post('/api/vc/break-glass', json=to_wire(override))
    assert response.status_code == 202
    assert response.json() == {'operation_id': uid(2), 'status': 'quarantined', 'job_run_id': None}
    assert client.get(f'/api/vc/operations/{uid(2)}/audit').status_code == 200
    authentication[0] = None
    assert client.get(f'/api/vc/operations/{uid(2)}').status_code == 401
    assert client.post('/api/vc/break-glass', json=to_wire(override)).status_code == 401


@pytest.fixture
def platform(request):
    try:
        return request.getfixturevalue('m06_platform')
    except pytest.FixtureLookupError:
        pytest.skip('DEPLOYMENT BLOCKER: supply the real M08 m06_platform fixture; no platform in offline sandbox')


@pytest.mark.integration
@pytest.mark.parametrize('kind', ['approval_request', 'receipt'])
def test_untrusted_source_cannot_insert_target_approvals_or_receipts(platform, kind):
    statement, parameters = platform.valid_insert(kind)
    platform.target_sql(statement, parameters)
    with pytest.raises(PermissionError):
        platform.source_sql(statement, parameters)
    with pytest.raises(PermissionError):
        platform.source_sql(f"ALTER TABLE {platform.operations_table} SET TBLPROPERTIES ('delta.appendOnly'='false')", {})
    assert platform.read_approved_export_as_source()
    with pytest.raises(PermissionError):
        platform.read_private_evidence_as_source()


@pytest.mark.integration
def test_real_delta_append_only_and_durable_replay(platform):
    ledger = platform.operation_facts()
    original = platform.valid_fact()
    reference = ledger.append(original)
    assert platform.operation_facts().append(original) == reference
    assert platform.count_event(original.event_key) == 1
    with pytest.raises(PermissionError):
        platform.target_sql(f'DELETE FROM {platform.operations_table} WHERE event_key = :key', {'key': original.event_key})
    with pytest.raises(PermissionError):
        platform.target_sql(f'UPDATE {platform.operations_table} SET status = :status WHERE event_key = :key',
                            {'status': 'confirmed', 'key': original.event_key})


@pytest.mark.integration
def test_real_target_sp_membership_revocation_and_human_identity(platform):
    service, request, executor, human = platform.approved_production_request()
    platform.identity.verify_run_as(executor.execution_ref, executor.principal_id)
    assert platform.authenticated_human().actor_kind == 'human'
    assert platform.authenticated_service().actor_kind == 'service'
    assert service.authorize(request, executor).approval_id == request.approval_id
    with platform.revoke_target_approver_membership(human):
        with pytest.raises(PermissionError):
            service.authorize(request, executor)


@pytest.mark.integration
def test_real_delta_and_evidence_volume_outage_disable_authorization(platform):
    service, request, executor, _ = platform.approved_production_request()
    with platform.deny_operation_reads():
        with pytest.raises((PermissionError, OSError)):
            service.authorize(request, executor)
    with platform.deny_private_evidence_reads():
        with pytest.raises((PermissionError, OSError)):
            service.authorize(request, executor)


def test_policy_reviewer_can_read_approval_and_invalid_inputs_are_422():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.routers.vc_approvals import build_router

    context = setup_approval()
    approval = approve(context)
    app = FastAPI()

    @app.middleware('http')
    async def authenticate(request, call_next):
        request.state.vc_auth = AuthenticatedRequest('human-1', '123')
        return await call_next(request)

    app.include_router(build_router(context.service, context.identity))
    client = TestClient(app)
    assert client.get(f'/api/vc/approvals/{approval.approval_id}').status_code == 200
    response = client.post('/api/vc/approvals', json={**to_wire(context.bound), 'authorization_grant': {}})
    assert response.status_code == 422
    context.identity.memberships.clear()
    assert client.get(f'/api/vc/approvals/{approval.approval_id}').status_code == 403

    def unavailable(*args):
        raise OSError('evidence unavailable')

    context.facts.get_request = unavailable
    response = client.get(f'/api/vc/approvals/{approval.approval_id}')
    assert response.status_code == 503
    assert response.json()['detail']['retryable'] is False
    assert response.json()['detail']['stale'] is True
