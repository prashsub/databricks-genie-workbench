"""Workbench-native approval policy over VC/1.0 ports."""

from backend.services.version_control.contracts import canonical_json_hash, to_wire


def input_digest(inputs):
    return canonical_json_hash('vc-approval-inputs/1', to_wire(inputs))


def approval_digest(inputs, votes):
    return canonical_json_hash('vc-approval/1', {
        'inputs': to_wire(inputs),
        'votes': to_wire(sorted(votes, key=lambda vote: vote.approver_id)),
    })


def request_digest(request):
    payload = to_wire(request)
    payload['identity'] = {'operation_id': request.identity.operation_id}
    return canonical_json_hash('vc-request/1', payload)
