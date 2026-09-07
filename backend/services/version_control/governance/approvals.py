"""Workbench-native approval policy over VC/1.0 ports."""

from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control.contracts import (
    ApprovalRecord, ApprovalRequest, ApprovalVote, AuthorizationGrant, FactKind, FactStatus,
    IdentityProvider, OperationFact, OperationFacts, Registry, canonical_json_hash, to_wire,
)
from .facts import fact_key


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


class ApprovalService:
    def __init__(self, facts: OperationFacts, identity: IdentityProvider, registry: Registry,
                 now, *, target_identity, writes_enabled=False):
        self.facts = facts
        self.identity = identity
        self.registry = registry
        self.now = now
        self.target_identity = target_identity
        self.writes_enabled = writes_enabled

    def _enabled(self):
        if not self.writes_enabled:
            raise PermissionError('Approval writes disabled')

    def get(self, approval_id):
        record = self.facts.get_request(approval_id).approval
        if record is None or record.request.approval_id != approval_id:
            raise LookupError('Approval evidence unavailable')
        return record

    def _record(self, record, actor, kind):
        inputs = record.request.inputs
        request = self.facts.get_request(inputs.operation_id).request
        provisional = OperationFact(
            event_id=inputs.operation_id, fact_kind=kind, operation_id=inputs.operation_id,
            transition_sequence=len(record.votes), event_key='', binding=inputs.target_binding,
            request=request.identity, operation_type=inputs.operation_type,
            requester_id=inputs.requester_id, actor=actor, status=record.status,
            evidence=record, recorded_at=self.now(), approval_id=record.request.approval_id,
            approval_digest=record.approval_digest,
        )
        key = fact_key(provisional)
        self.facts.append(replace(provisional, event_key=key, event_id=str(uuid5(NAMESPACE_URL, key))))

    def request(self, inputs, actor):
        self._enabled()
        if inputs.requester_id != actor.subject_id or actor.workspace_id != inputs.target_binding.workspace_id:
            raise PermissionError('Authenticated requester mismatch')
        stored = self.facts.get_request(inputs.operation_id)
        if stored.approval is not None:
            if stored.approval.request.inputs != inputs:
                raise ValueError('Approval inputs are immutable')
            return stored.approval.request
        request = ApprovalRequest(inputs.operation_id, inputs, self.now())
        self._record(ApprovalRecord(request, (), FactStatus.REQUESTED, None), actor, FactKind.APPROVAL_REQUEST)
        return request

    def vote(self, approval_id, decision, actor):
        self._enabled()
        record = self.get(approval_id)
        inputs = record.request.inputs
        if actor.actor_kind != 'human' or actor.subject_id == inputs.requester_id:
            raise PermissionError('Only non-requester humans may approve')
        if 'approvers' not in self.identity.groups(actor.subject_id, actor.workspace_id):
            raise PermissionError('Approver policy membership required')
        if decision not in ('approve', 'reject'):
            raise ValueError('Unknown vote decision')
        if actor.workspace_id != record.request.inputs.target_binding.workspace_id:
            raise PermissionError('Wrong voting workspace')
        prior = next((vote for vote in record.votes if vote.approver_id == actor.subject_id), None)
        if prior is not None:
            if prior.decision != decision:
                raise ValueError('Votes are immutable')
            return record
        votes = (*record.votes, ApprovalVote(actor.subject_id, decision, self.now()))
        updated = replace(record, votes=votes, approval_digest=approval_digest(record.request.inputs, votes))
        self._record(updated, actor, FactKind.APPROVAL_VOTE)
        return updated

    def authorize(self, request, executor):
        self._enabled()
        record = self.get(request.approval_id)
        inputs = record.request.inputs
        approvers = {vote.approver_id for vote in record.votes if vote.decision == 'approve'}
        if (len(approvers) < 2 or any(vote.decision != 'approve' for vote in record.votes)
                or inputs.requester_id in approvers or executor.principal_id in approvers):
            raise PermissionError('Two distinct non-requester non-deployer humans required')
        if any('approvers' not in self.identity.groups(subject, inputs.target_binding.workspace_id)
               for subject in approvers):
            raise PermissionError('Approver policy membership revoked')
        if executor.workspace_id != inputs.target_binding.workspace_id or executor.actor_kind != 'service':
            raise PermissionError('Target service executor required')
        target = self.target_identity(executor)
        target.verify_run_as(executor.execution_ref, executor.principal_id)
        if not any('target-approvers' in target.groups(subject, executor.workspace_id) for subject in approvers):
            raise PermissionError('Target-side approver membership required')
        return AuthorizationGrant(inputs.target_binding, request.identity, record.request.approval_id,
                                  inputs.expected_base_fingerprints, inputs.rendered_target_digest,
                                  inputs.expires_at, record.request.approval_id, record.approval_digest)
