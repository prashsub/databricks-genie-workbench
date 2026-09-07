from dataclasses import fields, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.services.version_control.contracts import (
    ActorContext, ApprovalInputs, ApprovalUse, ApprovalVote, AuthenticatedRequest, BreakGlassRequest,
    FactKind, FactStatus, Fingerprints, MutationRequest, ObservationRef, PatchStage, RequestHistory, StageEvidence,
    RequestIdentity, canonical_json_hash, from_wire, to_wire,
)
from backend.tests.test_vc_operation_facts import NOW, DIGEST, durable, fact, uid
from backend.tests.vc_fakes.fixtures import FakeIdentityProvider, binding_fixture, executor_fixture
from backend.tests.vc_fakes.stores import ManualClock


def inputs():
    fingerprints = Fingerprints(DIGEST, DIGEST, DIGEST, 'vc-c14n/1')
    return ApprovalInputs(
        'VC/1.0', uid(2), 'edit', uid(3), DIGEST, fingerprints, None, None,
        DIGEST, None, 'vc-c14n/1', binding_fixture(), fingerprints, None,
        DIGEST, DIGEST, DIGEST, {'minimum': 1}, 'requester',
        {'verify_only': True}, NOW + timedelta(hours=1),
    )


BOUND_PATHS = [field.name for field in fields(ApprovalInputs)] + [
    f'{parent}.{field.name}'
    for parent, value_type in [('target_binding', type(binding_fixture())),
                               ('source_fingerprints', Fingerprints),
                               ('expected_base_fingerprints', Fingerprints)]
    for field in fields(value_type)
]


@pytest.mark.parametrize('path', BOUND_PATHS)
def test_approval_digest_changes_for_every_bound_input(path):
    from backend.services.version_control.governance.approvals import approval_digest, input_digest

    original = inputs()
    payload = to_wire(original)
    container = payload
    names = path.split('.')
    for name in names[:-1]:
        container = container[name]
    name = names[-1]
    value = container[name]
    if path == 'schema_version':
        payload[name] = 'VC/2.0'
        with pytest.raises((ValueError, TypeError)):
            from_wire(ApprovalInputs, payload)
        return
    if isinstance(value, dict):
        if name == 'target_binding':
            container[name]['binding_revision'] += 1
        elif 'fingerprints' in name:
            container[name]['metadata'] = 'b' * 64
        else:
            container[name]['changed'] = True
    elif isinstance(value, int):
        container[name] += 1
    elif value is None:
        container[name] = 'b' * 64 if 'digest' in name else 'changed'
    elif name.endswith('_id') and value.startswith('00000000-'):
        container[name] = uid(99)
    elif name == 'expires_at':
        container[name] = to_wire(NOW + timedelta(hours=2))
    elif len(value) == 64:
        container[name] = 'b' * 64
    else:
        container[name] = value + '-changed'
    changed = from_wire(ApprovalInputs, payload)
    assert input_digest(original) != input_digest(changed)
    vote = ApprovalVote('human-1', 'approve', NOW)
    assert approval_digest(original, (vote,)) != approval_digest(changed, (vote,))
    assert approval_digest(original, (vote,)) != approval_digest(original, (replace(vote, approver_id='human-2'),))
    assert approval_digest(original, (vote,)) != approval_digest(original, (replace(vote, approved_at=NOW + timedelta(seconds=1)),))


def test_request_digest_is_distinct_from_idempotency_key_and_payload_is_immutable():
    from backend.services.version_control.governance.approvals import request_digest

    request = MutationRequest(RequestIdentity(uid(2), 'key', DIGEST), binding_fixture(),
                              'edit', uid(3), DIGEST, {'nested': [1]}, None, uid(2))
    assert request_digest(request) == request_digest(replace(request, identity=replace(request.identity, idempotency_key='another')))
    assert request_digest(request) != request_digest(replace(request, description='changed'))
    with pytest.raises(TypeError):
        request.serialized_space['nested'] = []


def setup_approval(environment='prod', operation_type='edit'):
    from backend.services.version_control.governance.approvals import ApprovalService, request_digest

    ledger, store = durable()
    identity = FakeIdentityProvider()
    target_identity = FakeIdentityProvider()
    clock = ManualClock(NOW)
    bound = replace(inputs(), target_binding=replace(binding_fixture(), environment=environment),
                    operation_type=operation_type)
    request = MutationRequest(RequestIdentity(uid(2), 'key', DIGEST), bound.target_binding,
                              operation_type, uid(3), bound.expected_base_fingerprints.state_digest,
                              {'instructions': []}, None, uid(2))
    bound = replace(bound, rendered_target_digest=canonical_json_hash(
        'vc-rendered-target/1', {'serialized_space': request.serialized_space, 'description': request.description}))
    request = replace(request, identity=replace(request.identity, request_digest=request_digest(request)))
    ledger.record_request(request, fact(binding=request.binding, request=request.identity,
                                       requester_id='requester', operation_type=operation_type))
    registry = SimpleNamespace(binding=bound.target_binding)
    registry.resolve = lambda binding_id: registry.binding
    for subject in ('human-1', 'human-2', 'requester'):
        identity.actors[subject] = ActorContext(subject, '123', 'human')
        identity.memberships[(subject, '123')] = frozenset({'approvers'})
        target_identity.memberships[(subject, '123')] = frozenset({'target-approvers'})
    identity.edit_rights.add(('requester', '123', request.binding.binding_id, 1))
    executor = executor_fixture()
    target_identity.run_as[executor.execution_ref] = executor.principal_id
    service = ApprovalService(ledger, identity, registry, clock.now,
                              target_identity=lambda selected: target_identity, writes_enabled=True)
    return SimpleNamespace(service=service, facts=ledger, store=store, identity=identity,
                           target_identity=target_identity, clock=clock, bound=bound,
                           request=request, registry=registry, executor=executor)


def submit(context):
    return context.service.request(context.bound, context.identity.actors['requester'])


def test_vote_identity_is_server_derived_not_request_body():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.routers.vc_approvals import build_router

    context = setup_approval()
    approval = submit(context)
    app = FastAPI()

    @app.middleware('http')
    async def authenticate(request, call_next):
        request.state.vc_auth = AuthenticatedRequest('human-1', '123')
        return await call_next(request)

    app.include_router(build_router(context.service, context.identity))
    client = TestClient(app)
    url = f'/api/vc/approvals/{approval.approval_id}/votes'
    assert client.post(url, json={'decision': 'approve', 'actor_id': 'human-2'}).status_code == 422
    response = client.post(url, json={'decision': 'approve'})
    assert response.status_code == 200
    assert response.json()['votes'][0]['approver_id'] == 'human-1'
    record = context.service.get(approval.approval_id)
    assert record.votes[0].approver_id == 'human-1'
    assert context.service.vote(approval.approval_id, 'approve', context.identity.actors['human-1']) == record
    with pytest.raises(ValueError, match='immutable'):
        context.service.vote(approval.approval_id, 'reject', context.identity.actors['human-1'])
    with pytest.raises(PermissionError, match='requester'):
        context.service.request(replace(context.bound, requester_id='spoof'), context.identity.actors['requester'])


@pytest.mark.parametrize('invalid', ['missing', 'duplicate', 'requester', 'service', 'unauthorized', 'deployer', 'reject'])
def test_production_requires_two_distinct_nonrequester_humans(invalid):
    context = setup_approval()
    approval = submit(context)
    first = context.identity.actors['human-1']
    context.service.vote(approval.approval_id, 'approve', first)
    if invalid in ('requester', 'service', 'unauthorized'):
        actor = context.identity.actors['requester'] if invalid == 'requester' else ActorContext('bot', '123', 'service') if invalid == 'service' else ActorContext('outsider', '123', 'human')
        with pytest.raises(PermissionError):
            context.service.vote(approval.approval_id, 'approve', actor)
    if invalid == 'duplicate':
        context.service.vote(approval.approval_id, 'approve', first)
    if invalid in ('reject', 'deployer'):
        context.service.vote(approval.approval_id, 'reject' if invalid == 'reject' else 'approve', context.identity.actors['human-2'])
    executor = replace(context.executor, principal_id='human-2') if invalid == 'deployer' else context.executor
    with pytest.raises(PermissionError):
        context.service.authorize(context.request, executor)
    if invalid not in ('reject', 'deployer'):
        context.service.vote(approval.approval_id, 'approve', context.identity.actors['human-2'])
        grant = context.service.authorize(context.request, context.executor)
        assert grant.approval_id == approval.approval_id
        assert grant.binding == context.bound.target_binding
        assert grant.request == context.request.identity
        assert grant.expected_base_fingerprints == context.bound.expected_base_fingerprints


def approve(context):
    approval = submit(context)
    for subject in ('human-1', 'human-2'):
        context.service.vote(approval.approval_id, 'approve', context.identity.actors[subject])
    return approval


@pytest.mark.parametrize('fault', ['revoked', 'source-only', 'wrong-workspace', 'wrong-run-as', 'human-executor', 'outage'])
def test_target_membership_rechecked_by_target_executor_at_execution(fault):
    context = setup_approval()
    approve(context)
    seen = []

    def target_provider(executor):
        seen.append(executor)
        return context.target_identity

    context.service.target_identity = target_provider
    context.target_identity.memberships.pop(('human-2', '123'))
    assert context.service.authorize(context.request, context.executor).approval_id == uid(2)
    assert seen == [context.executor]
    executor = context.executor
    if fault in ('revoked', 'source-only'):
        context.target_identity.memberships.clear()
        context.identity.memberships[('human-1', 'source')] = frozenset({'target-approvers'})
    elif fault == 'wrong-workspace':
        executor = replace(executor, workspace_id='source')
    elif fault == 'wrong-run-as':
        context.target_identity.run_as[executor.execution_ref] = 'another-service'
    elif fault == 'human-executor':
        executor = replace(executor, actor_kind='human')
    else:
        def unavailable(*args):
            raise OSError('identity evidence unavailable')
        context.target_identity.groups = unavailable
    with pytest.raises((PermissionError, OSError)):
        context.service.authorize(context.request, executor)


@pytest.mark.parametrize('fault', ['over-24h', 'expired', 'expired-vote', 'stale-request', 'stale-execution', 'exact-24h'])
def test_expiry_over_24_hours_and_stale_binding_revision_are_rejected(fault):
    context = setup_approval()
    if fault == 'over-24h':
        context.bound = replace(context.bound, expires_at=NOW + timedelta(hours=24, seconds=1))
        with pytest.raises(PermissionError, match='expiry'):
            submit(context)
    elif fault == 'stale-request':
        context.registry.binding = replace(context.registry.binding, binding_revision=2)
        with pytest.raises(PermissionError, match='binding'):
            submit(context)
    elif fault == 'expired-vote':
        approval = submit(context)
        context.clock.advance(timedelta(hours=1))
        with pytest.raises(PermissionError, match='expiry'):
            context.service.vote(approval.approval_id, 'approve', context.identity.actors['human-1'])
    else:
        if fault == 'exact-24h':
            context.bound = replace(context.bound, expires_at=NOW + timedelta(hours=24))
        approve(context)
        if fault == 'expired':
            context.clock.advance(timedelta(hours=1))
        elif fault == 'stale-execution':
            context.registry.binding = replace(context.registry.binding, binding_revision=2)
        if fault == 'exact-24h':
            assert context.service.authorize(context.request, context.executor).expires_at == context.bound.expires_at
        else:
            with pytest.raises(PermissionError, match='expiry|binding'):
                context.service.authorize(context.request, context.executor)


def test_approval_writes_default_off_and_non_utc_expiry_rejected():
    from backend.services.version_control.governance.approvals import ApprovalService

    context = setup_approval()
    service = ApprovalService(context.facts, context.identity, context.registry, context.clock.now,
                              target_identity=lambda executor: context.target_identity)
    with pytest.raises(PermissionError, match='disabled'):
        service.request(context.bound, context.identity.actors['requester'])
    with pytest.raises(ValueError, match='UTC'):
        replace(context.bound, expires_at=NOW.replace(tzinfo=None))


def test_dev_grant_requires_requester_edit_right_but_release_requires_policy_group():
    context = setup_approval(environment='dev')
    submit(context)
    grant = context.service.authorize(context.request, context.executor)
    assert grant.authorization_reference.startswith('rights:')
    assert grant.approval_id is None
    context.identity.edit_rights.clear()
    with pytest.raises(PermissionError, match='edit right'):
        context.service.authorize(context.request, context.executor)
    release = setup_approval(environment='dev', operation_type='release')
    approve(release)
    with pytest.raises(PermissionError, match='release policy'):
        release.service.authorize(release.request, release.executor)
    release.identity.memberships[('requester', '123')] = frozenset({'release-requesters'})
    assert release.service.authorize(release.request, release.executor).approval_id == uid(2)
    release.target_identity.memberships.clear()
    with pytest.raises(PermissionError, match='Target-side'):
        release.service.authorize(release.request, release.executor)


@pytest.mark.parametrize('fault', ['consumed', 'different-digest', 'same-key-new-operation',
                                  'payload', 'metadata', 'ambiguous', 'missing', 'outage', 'completed'])
def test_historical_approval_reuse_and_different_request_digest_are_rejected(fault):
    context = setup_approval()
    approval = approve(context)
    request = context.request
    if fault == 'consumed':
        record = context.service.get(approval.approval_id)
        context.facts.append(fact(
            binding=request.binding, request=request.identity, requester_id='requester',
            fact_kind=FactKind.APPROVAL_CONSUMED, status=FactStatus.CONSUMED,
            approval_id=approval.approval_id, approval_digest=record.approval_digest,
            attempt_id=uid(5), generation=1, evidence=record,
        ))
        context.facts.append(fact(binding=request.binding, operation_id=uid(20),
                                 request=RequestIdentity(uid(20), 'successor', DIGEST)))
    elif fault == 'different-digest':
        request = replace(request, identity=replace(request.identity, request_digest='b' * 64))
    elif fault == 'same-key-new-operation':
        request = replace(request, identity=replace(request.identity, operation_id=uid(20)))
    elif fault == 'payload':
        request = replace(request, serialized_space={'tampered': True})
    elif fault == 'metadata':
        request = replace(request, description='unreviewed')
    elif fault == 'ambiguous':
        context.facts.approval_use = lambda binding, approval_id: ApprovalUse(binding, approval_id, (), (), True)
    elif fault == 'missing':
        context.facts.lookup_request = lambda *args: RequestHistory((), False)
    elif fault == 'outage':
        def unavailable(*args):
            raise OSError('fact evidence unavailable')
        context.facts.approval_use = unavailable
    else:
        context.facts.append(fact(binding=request.binding, request=request.identity,
                                 transition_sequence=10, status=FactStatus.CONFIRMED))
    with pytest.raises((PermissionError, ValueError, OSError)):
        context.service.authorize(request, context.executor)


def test_approval_request_rejects_inputs_not_bound_to_durable_request():
    context = setup_approval()
    with pytest.raises(ValueError, match='bound'):
        context.service.request(replace(context.bound, rendered_target_digest='b' * 64),
                                context.identity.actors['requester'])


@pytest.mark.parametrize('action', ['adopt', 'reapply'])
def test_adopt_and_reapply_need_new_approval_for_newly_captured_base(action):
    from backend.services.version_control.governance.approvals import request_digest

    context = setup_approval(operation_type=action)
    with pytest.raises(PermissionError, match='captured base'):
        submit(context)
    observation = ObservationRef(uid(3), context.request.binding.binding_id, 1,
                                 context.request.expected_base, DIGEST)
    capture = fact(binding=context.request.binding, request=context.request.identity,
                   operation_type=action, transition_sequence=1,
                   status=FactStatus.PREIMAGE_CAPTURED, pre_version_id=observation.version_id,
                   evidence=StageEvidence('VC/1.0', PatchStage.CONFIG_PENDING, NOW, DIGEST, observation))
    context.facts.append(capture)
    assert context.facts.get_request(uid(2)).approval is None
    approve(context)
    assert context.service.authorize(context.request, context.executor).approval_id == uid(2)
    new_base = replace(context.bound.expected_base_fingerprints, metadata='b' * 64)
    new_request = replace(context.request, identity=RequestIdentity(uid(22), 'new-base', DIGEST),
                          expected_base=new_base.state_digest)
    new_request = replace(new_request, identity=replace(new_request.identity, request_digest=request_digest(new_request)))
    context.facts.record_request(new_request, fact(operation_id=uid(22), binding=new_request.binding,
                                                 request=new_request.identity, operation_type=action))
    new_inputs = replace(context.bound, operation_id=uid(22), expected_base_fingerprints=new_base)
    with pytest.raises((ValueError, PermissionError)):
        context.service.request(new_inputs, context.identity.actors['requester'])
    with pytest.raises((ValueError, PermissionError)):
        context.service.authorize(new_request, context.executor)


@pytest.mark.parametrize('fault', ['group', 'reason', 'expiry', 'over-24h', 'scope', 'evidence', 'acknowledgement', 'outage', 'none'])
def test_break_glass_requires_group_reason_expiry_and_cannot_skip_evidence(fault):
    context = setup_approval()
    approve(context)
    actor = ActorContext('emergency-human', '123', 'human')
    context.identity.memberships[(actor.subject_id, '123')] = frozenset({'break-glass'})
    request = BreakGlassRequest(context.request.identity, context.request.binding, 'incident-42',
                                NOW + timedelta(minutes=15), context.bound.preflight_evidence_digest, True)
    if fault == 'group':
        context.identity.memberships[(actor.subject_id, '123')] = frozenset({'approvers'})
    elif fault == 'reason':
        request = replace(request, reason='  ')
    elif fault == 'expiry':
        request = replace(request, expires_at=NOW)
    elif fault == 'over-24h':
        request = replace(request, expires_at=NOW + timedelta(hours=25))
    elif fault == 'scope':
        request = replace(request, binding=replace(request.binding, workspace_id='other'))
    elif fault == 'evidence':
        request = replace(request, evidence_digest='b' * 64)
    elif fault == 'acknowledgement':
        request = replace(request, residual_risk_acknowledged=False)
    elif fault == 'outage':
        def unavailable(*args):
            raise OSError('Delta unavailable')
        context.facts.append = unavailable
    if fault != 'none':
        with pytest.raises((PermissionError, ValueError, OSError)):
            context.service.break_glass(request, actor)
    else:
        grant = context.service.break_glass(request, actor)
        assert grant.authorization_reference.startswith('break-glass:')
        assert grant.expected_base_fingerprints == context.bound.expected_base_fingerprints
        assert context.service.suspended(request.binding)
        assert context.service.break_glass(request, actor) == grant
        with pytest.raises(PermissionError, match='reconciliation'):
            context.service.authorize(context.request, context.executor)
        assert any(row.payload['drift_override'] and row.payload['status'] == 'quarantined'
                   for row in context.store.state.rows)
