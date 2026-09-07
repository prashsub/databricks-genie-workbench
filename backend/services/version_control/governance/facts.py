"""Append-only durable projections. Inject target-owned storage, never a cache.

Storage callbacks must read committed Delta rows and propagate failures. Logical
keys are not Delta uniqueness constraints: conflicting duplicates fail closed.
Admission serialization remains exclusively the Coordination port's concern.
"""

from collections.abc import Callable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control.contracts import (
    FactRef, OperationFact, canonical_json_hash, from_wire, to_wire,
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
        return tuple(from_wire(OperationFact, row) for row in self._read())

    def append(self, fact: OperationFact) -> FactRef:
        if not self.writes_enabled:
            raise PermissionError('Fact writes disabled')
        fact = from_wire(OperationFact, to_wire(fact))
        if fact.operation_id != fact.request.operation_id:
            raise ValueError('Operation identity mismatch')
        if fact.event_key != fact_key(fact):
            raise ValueError('Invalid deterministic event key')
        if fact.event_id != str(uuid5(NAMESPACE_URL, fact.event_key)):
            raise ValueError('Invalid deterministic event identity')
        existing = tuple(row for row in self._rows() if row.event_key == fact.event_key)
        if any(row != fact for row in existing):
            raise ValueError('Fact conflict')
        if not existing:
            self._insert(fact.event_key, to_wire(fact))
        persisted = tuple(row for row in self._rows() if row.event_key == fact.event_key)
        if not persisted or any(row != fact for row in persisted):
            raise ValueError('Fact conflict or unavailable committed evidence')
        return FactRef(fact.event_id, fact.event_key,
                       canonical_json_hash('vc-fact-evidence/1', to_wire(fact.evidence)))
