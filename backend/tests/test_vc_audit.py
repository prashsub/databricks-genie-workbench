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
