"""Synchronous, fail-closed coordination. Never sends a Genie mutation."""
from datetime import timedelta
from uuid import uuid4

from backend.services.version_control import contracts as c
from .row import CoordinationRow


class CoordinationError(RuntimeError):
    """No write authority may be inferred from a failed coordination call."""


class OwnershipError(CoordinationError):
    pass


class AuthorityUnavailable(CoordinationError):
    pass


class CoordinationService:
    def __init__(self, *, store, facts: c.OperationFacts, ledger: c.VersionLedger,
                 clock, resolve_binding, verify_enrollment, insert_enrolled,
                 validate_authorization, verify_create_intent,
                 termination: c.TerminationEvidenceProvider,
                 authorize_human_recovery, authorize_heads):
        self.store = store
        self.facts = facts
        self.ledger = ledger
        self.clock = clock
        self.resolve_binding = resolve_binding
        self.verify_enrollment = verify_enrollment
        self.insert_enrolled = insert_enrolled
        self.validate_authorization = validate_authorization
        self.verify_create_intent = verify_create_intent
        self.termination = termination
        self.authorize_human_recovery = authorize_human_recovery
        self.authorize_heads = authorize_heads

    @staticmethod
    def _io(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except CoordinationError:
            raise
        except Exception as exc:
            raise AuthorityUnavailable('Durable authority unavailable or ambiguous') from exc

    def _current(self, binding):
        if self._io(self.resolve_binding, binding.binding_id) != binding:
            raise OwnershipError('Not the unique current binding/revision')

    def _row(self, binding):
        self._current(binding)
        row = self._io(self.store.read, binding.binding_id)
        if row is None or row.binding != binding:
            raise OwnershipError('Missing ownership row or revision mismatch')
        return row

    def initialize(self, binding: c.BindingRef, enrollment: c.EnrollmentProof) -> None:
        # The callback verifies a held serialized enrollment authority, including
        # unique physical ownership. A proof string alone is not a lock.
        if ((enrollment.binding_id, enrollment.binding_revision) !=
                (binding.binding_id, binding.binding_revision)
                or not self._io(self.verify_enrollment, binding, enrollment)):
            raise OwnershipError('Serialized enrollment proof required')
        self._current(binding)
        row = self._io(self.store.read, binding.binding_id)
        if row is None:
            self._io(self.insert_enrolled, CoordinationRow(binding, self.clock.now()))
        self._row(binding)

    def _cas(self, row, predicate, **changes):
        result = self._io(self.store.compare_and_swap, row.binding.binding_id,
                          row.binding.binding_revision, row.row_version,
                          row.generation, predicate, **changes)
        if result is None:
            raise OwnershipError('Serializable CAS lost; do not retry acquisition')
        observed = self._row(row.binding)
        if (observed.row_version != row.row_version + 1
                or observed.holder != changes.get('holder', row.holder)
                or observed.attempt_id != changes.get('attempt_id', row.attempt_id)
                or observed.generation != changes.get('generation', row.generation)
                or any(getattr(observed, key) != value for key, value in changes.items())):
            raise AuthorityUnavailable('CAS read-back did not prove exact ownership/claim')
        return observed

    @staticmethod
    def _fence(row):
        return c.FenceToken(row.binding.binding_id, row.binding.binding_revision,
                            row.attempt_id, row.generation, row.row_version)

    def _fact(self, row, request, status, *, evidence=None, kind=c.FactKind.OPERATION,
              post_version_id=None):
        stage = row.mutation_stage or c.PatchStage.CONFIG_PENDING
        if evidence is None:
            evidence = c.StageEvidence('VC/1.0', stage, self.clock.now(),
                c.canonical_json_hash('vc-coordination/1', {
                    'status': status.value, 'attempt': row.attempt_id,
                    'row_version': row.row_version}), None)
        key = f'{request.operation_id}:{row.attempt_id}:{row.row_version}:{status.value}'
        fact = c.OperationFact(str(uuid4()), kind, request.operation_id,
            row.row_version, key, row.binding, request, 'coordination',
            row.holder or 'coordination',
            c.ActorContext(row.holder or 'coordination', row.binding.workspace_id,
                           row.executor_kind or 'service'), status, evidence,
            self.clock.now(), attempt_id=row.attempt_id, generation=row.generation,
            pre_version_id=row.pre_version_id, post_version_id=post_version_id,
            approval_id=row.approval_id, approval_digest=row.approval_digest)
        return self._io(self.facts.append, fact)

    def reserve(self, binding: c.BindingRef, operation: c.RequestIdentity,
                executor: c.ExecutorContext) -> c.Reservation:
        row = self._row(binding)
        if executor.workspace_id != binding.workspace_id or not executor.execution_ref:
            raise OwnershipError('Explicit target-local executor required')
        try:
            if row.state != c.CoordinationState.IDLE or row.unresolved:
                raise OwnershipError('An unresolved attempt already owns this binding')
            row = self._cas(row,
                lambda r: r.state == c.CoordinationState.IDLE and not r.unresolved,
                state=c.CoordinationState.RESERVED, unresolved=True,
                generation=row.generation + 1, attempt_id=str(uuid4()),
                holder=executor.principal_id, executor_kind=executor.actor_kind,
                executor_ref=executor.execution_ref,
                lease_expires_at=self.clock.now() + timedelta(seconds=60),
                active_operation_id=operation.operation_id,
                idempotency_key=operation.idempotency_key, request_digest=operation.request_digest)
        except OwnershipError:
            self._fact(row, operation, c.FactStatus.CONFLICTED)
            raise
        return c.Reservation(self._fence(row), operation, executor, row.lease_expires_at)
