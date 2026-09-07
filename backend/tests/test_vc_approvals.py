from dataclasses import fields, replace
from datetime import timedelta

import pytest

from backend.services.version_control.contracts import (
    ActorContext, ApprovalInputs, ApprovalVote, Fingerprints, MutationRequest,
    RequestIdentity, from_wire, to_wire,
)
from backend.tests.test_vc_operation_facts import NOW, DIGEST, uid
from backend.tests.vc_fakes.fixtures import binding_fixture


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
