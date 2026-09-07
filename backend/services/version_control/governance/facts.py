"""Append-only durable projections. Inject target-owned storage, never a cache.

Storage callbacks must read committed Delta rows and propagate failures. Logical
keys are not Delta uniqueness constraints: conflicting duplicates fail closed.
Admission serialization remains exclusively the Coordination port's concern.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control.contracts import (
    ActorContext, AdmissionClaim, ApprovalRecord, ApprovalUse, ApprovedOperation,
    CreateIntentRef, CreateRequest, FactKind, FactStatus, FactRef, MutationRequest,
    OperationFact, RequestHistory,
    canonical_json_hash, from_wire, to_wire,
)


def fact_key(fact: OperationFact) -> str:
    return canonical_json_hash('vc-fact-key/1', {
        'binding': to_wire(fact.binding), 'operation_id': fact.operation_id,
        'kind': fact.fact_kind.value, 'sequence': fact.transition_sequence,
        'attempt_id': fact.attempt_id, 'generation': fact.generation,
    })


class DurableOperationFacts:
    def __init__(self, read: Callable[[], Sequence[Mapping]],
                 insert: Callable[[str, Mapping], object], *, writes_enabled=False):
        self._read = read
        self._insert = insert
        self.writes_enabled = writes_enabled

    def _rows(self):
        return tuple(from_wire(OperationFact, {key: value for key, value in row.items()
                                              if key not in ('operation_request', 'admission_claim')})
                     for row in self._read())

    def append(self, fact: OperationFact) -> FactRef:
        return self._append(fact)

    def record_request(self, request: MutationRequest | CreateRequest, fact: OperationFact) -> FactRef:
        if request.identity != fact.request or request.binding != fact.binding:
            raise ValueError('Request identity mismatch')
        return self._append(fact, request)

    def _append(self, fact, request=None, claim=None):
        if not self.writes_enabled:
            raise PermissionError('Fact writes disabled')
        fact = from_wire(OperationFact, to_wire(fact))
        if fact.operation_id != fact.request.operation_id:
            raise ValueError('Operation identity mismatch')
        if fact.fact_kind == FactKind.CREATE_INTENT:
            expected = CreateIntentRef(fact.event_id, fact.operation_id, fact.binding.binding_id,
                                       fact.binding.binding_revision, fact.request.request_digest)
            if (fact.evidence != expected or fact.pre_version_id is not None
                    or fact.binding.space_id is not None or fact.operation_type != 'create'):
                raise ValueError('Invalid create intent evidence')
        if fact.event_key != fact_key(fact):
            raise ValueError('Invalid deterministic event key')
        if fact.event_id != str(uuid5(NAMESPACE_URL, fact.event_key)):
            raise ValueError('Invalid deterministic event identity')
        history = self.lookup_request(fact.binding, fact.request.idempotency_key)
        if history.ambiguous or any(row.request != fact.request for row in history.facts):
            raise ValueError('Conflicting idempotency key or request digest')
        existing = tuple(row for row in self._rows() if row.event_key == fact.event_key)
        if any(row != fact for row in existing):
            raise ValueError('Fact conflict')
        if not existing:
            payload = to_wire(fact)
            if request is not None:
                payload['operation_request'] = to_wire(request)
            if claim is not None:
                payload['admission_claim'] = to_wire(claim)
            self._insert(fact.event_key, payload)
        if request is not None and self.get_request(request.identity.operation_id).request != request:
            raise ValueError('Immutable request conflict')
        persisted = tuple(row for row in self._rows() if row.event_key == fact.event_key)
        if not persisted or any(row != fact for row in persisted):
            raise ValueError('Fact conflict or unavailable committed evidence')
        return FactRef(fact.event_id, fact.event_key,
                       canonical_json_hash('vc-fact-evidence/1', to_wire(fact.evidence)))

    def lookup_request(self, binding, idempotency_key):
        rows = tuple(row for row in self._rows() if row.binding.binding_id == binding.binding_id
                     and row.binding.binding_revision == binding.binding_revision
                     and row.binding.workspace_id == binding.workspace_id
                     and row.request.idempotency_key == idempotency_key)
        identities = {row.request for row in rows}
        by_key = {}
        conflict = False
        for row in rows:
            if row.event_key in by_key and row != by_key[row.event_key]:
                conflict = True
            by_key[row.event_key] = row
        return RequestHistory(tuple(sorted(by_key.values(), key=lambda row: row.transition_sequence)),
                              conflict or len(identities) > 1)

    def approval_use(self, binding, approval_id):
        rows = tuple(row for row in self._rows() if row.binding == binding
                     and row.approval_id == approval_id and row.fact_kind == FactKind.APPROVAL_CONSUMED)
        unique = {row.event_key: row for row in rows}
        operations = tuple(sorted({row.operation_id for row in rows}))
        ambiguous = len(operations) > 1 or any(unique[row.event_key] != row for row in rows)
        refs = tuple(FactRef(row.event_id, row.event_key,
                            canonical_json_hash('vc-fact-evidence/1', to_wire(row.evidence)))
                     for row in unique.values())
        return ApprovalUse(binding, approval_id, refs, operations, ambiguous)

    def get_request(self, operation_id):
        requests = []
        for row in self._read():
            if row['operation_id'] == operation_id and 'operation_request' in row:
                payload = row['operation_request']
                kind = CreateRequest if 'payload' in payload else MutationRequest
                requests.append(from_wire(kind, payload))
        if not requests:
            raise LookupError('Durable request evidence unavailable')
        if any(request != requests[0] for request in requests):
            raise ValueError('Ambiguous durable request')
        records = [row for row in self._rows() if row.operation_id == operation_id
                   and isinstance(row.evidence, ApprovalRecord)]
        records.sort(key=lambda row: row.transition_sequence)
        if records and any(row.transition_sequence == records[-1].transition_sequence
                           and row.evidence != records[-1].evidence for row in records):
            raise ValueError('Ambiguous approval evidence')
        return ApprovedOperation(requests[0], records[-1].evidence if records else None)

    def _claims(self, operation_id):
        return tuple((from_wire(AdmissionClaim, row['admission_claim']), row)
                     for row in self._read() if row['operation_id'] == operation_id
                     and row['fact_kind'] == FactKind.APPROVAL_CONSUMED.value
                     and 'admission_claim' in row)

    def verify_flush(self, claim):
        claims = self._claims(claim.request.operation_id)
        return bool(claims) and all(saved == claim for saved, row in claims)

    def publish_consumption(self, claim):
        if not self.writes_enabled:
            raise PermissionError('Fact writes disabled')
        existing = self._claims(claim.request.operation_id)
        if existing:
            if not self.verify_flush(claim):
                raise PermissionError('Conflicting consumed admission claim')
            payload = existing[0][1]
            return FactRef(payload['event_id'], payload['event_key'],
                           canonical_json_hash('vc-fact-evidence/1', payload['evidence']))
        stored = self.get_request(claim.request.operation_id)
        request = stored.request
        record = stored.approval
        if (request.identity != claim.request or request.binding.binding_id != claim.binding_id
                or request.binding.binding_revision != claim.binding_revision
                or claim.preimage.binding_id != claim.binding_id
                or claim.preimage.binding_revision != claim.binding_revision):
            raise ValueError('Admission claim binding or request mismatch')
        if record is None:
            raise PermissionError('Durable approval evidence does not match consumption')
        evidence = record
        if claim.approval_id is None:
            if (claim.approval_digest is not None or request.binding.environment != 'dev'
                    or request.operation_type != 'edit' or record.status != FactStatus.APPROVED):
                raise PermissionError('Durable dev grant evidence required')
        else:
            normal = (record.request.approval_id == claim.approval_id
                      and record.approval_digest == claim.approval_digest and record.status == FactStatus.APPROVED)
            if not normal:
                overrides = [row for row in self.lookup_request(request.binding, 'vc:break-glass-suspension').facts
                             if row.fact_kind == FactKind.BREAK_GLASS and row.approval_id == claim.approval_id
                             and row.approval_digest == claim.approval_digest
                             and row.evidence.identity == claim.request and row.evidence.binding == request.binding]
                if len(overrides) != 1:
                    raise PermissionError('Durable approval evidence does not match consumption')
                evidence = overrides[0].evidence
            use = self.approval_use(request.binding, claim.approval_id)
            if use.ambiguous or use.consumptions:
                raise PermissionError('Approval already consumed')
        pre_version_id = getattr(claim.preimage, 'version_id', None)
        if isinstance(request, MutationRequest) and getattr(claim.preimage, 'state_digest', None) != request.expected_base:
            raise ValueError('Admission preimage differs from reviewed base')
        provisional = OperationFact(
            event_id=request.identity.operation_id, fact_kind=FactKind.APPROVAL_CONSUMED,
            operation_id=request.identity.operation_id, transition_sequence=0, event_key='',
            binding=request.binding, request=request.identity, operation_type=request.operation_type,
            requester_id=record.request.inputs.requester_id,
            actor=ActorContext('coordination', request.binding.workspace_id, 'system'),
            status=FactStatus.CONSUMED, evidence=evidence, recorded_at=datetime.now(timezone.utc),
            attempt_id=claim.attempt_id, generation=claim.generation, pre_version_id=pre_version_id,
            approval_id=claim.approval_id, approval_digest=claim.approval_digest,
        )
        key = fact_key(provisional)
        reference = self._append(replace(provisional, event_key=key, event_id=str(uuid5(NAMESPACE_URL, key))), claim=claim)
        if not self.verify_flush(claim):
            raise PermissionError('Consumption flush unavailable')
        return reference
