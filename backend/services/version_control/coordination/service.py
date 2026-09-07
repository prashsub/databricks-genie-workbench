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
        self._history(row, operation)
        return c.Reservation(self._fence(row), operation, executor, row.lease_expires_at)

    def _owned(self, fence, *, allow_quarantine=False):
        binding = self._io(self.resolve_binding, fence.binding_id)
        row = self._row(binding)
        if ((row.binding.binding_revision, row.attempt_id, row.generation) !=
                (fence.binding_revision, fence.attempt_id, fence.generation)
                or fence.row_version > row.row_version or not row.unresolved
                or row.state == c.CoordinationState.IDLE
                or (row.state == c.CoordinationState.QUARANTINED and not allow_quarantine)):
            raise OwnershipError('Stale attempt/revision/generation or quarantined claim')
        if not allow_quarantine and (row.lease_expires_at is None or
                                    row.lease_expires_at <= self.clock.now()):
            raise OwnershipError('Expired lease; no takeover or renewal')
        return row

    def renew(self, claim: c.FenceToken) -> c.FenceToken:
        row = self._owned(claim)
        updated = self._cas(row, lambda r: r.attempt_id == claim.attempt_id,
                           lease_expires_at=self.clock.now() + timedelta(seconds=60))
        return self._fence(updated)

    def assert_owner(self, claim: c.FenceToken) -> None:
        row = self._owned(claim)
        if not isinstance(claim, c.AdmissionClaim) or row.state != c.CoordinationState.ADMITTED:
            raise OwnershipError('Reservation/observation is not write admission')
        if (claim.request != self._request(row)
                or (claim.approval_id, claim.approval_digest) != (row.approval_id, row.approval_digest)
                or (isinstance(claim.preimage, c.ObservationRef) and
                    (claim.preimage.version_id, claim.preimage.state_digest) !=
                    (row.pre_version_id, row.preimage_digest))
                or (isinstance(claim.preimage, c.CreateIntentRef) and
                    claim.preimage.event_id != row.create_intent_event_id)):
            raise OwnershipError('Admission claim differs from durable row')

    @staticmethod
    def _request(row):
        return c.RequestIdentity(row.active_operation_id, row.idempotency_key, row.request_digest)

    def admit(self, reservation: c.Reservation,
              preimage: c.ObservationRef | c.CreateIntentRef,
              authorization: c.AuthorizationGrant) -> c.AdmissionClaim:
        row = self._owned(reservation.fence)
        if row.state != c.CoordinationState.RESERVED:
            raise OwnershipError('Only a reservation can be admitted; no replay')
        if (reservation.request != self._request(row)
                or reservation.executor.principal_id != row.holder
                or reservation.executor.execution_ref != row.executor_ref
                or authorization.binding != row.binding
                or authorization.request != reservation.request
                or authorization.expires_at <= self.clock.now()
                or bool(authorization.approval_id) != bool(authorization.approval_digest)
                or not self._io(self.validate_authorization, authorization, reservation.executor)):
            raise CoordinationError('Authorization is stale, mismatched or revoked')
        if (preimage.binding_id, preimage.binding_revision) != (
                row.binding.binding_id, row.binding.binding_revision):
            raise OwnershipError('Preimage belongs to another binding/revision')
        if isinstance(preimage, c.ObservationRef):
            if not self._io(self.ledger.verify_committed, preimage):
                raise CoordinationError('Preimage not committed')
            if preimage.state_digest != authorization.expected_base_fingerprints.state_digest:
                self._fact(row, reservation.request, c.FactStatus.CONFLICTED)
                raise CoordinationError('Committed preimage differs from reviewed base')
            evidence = dict(pre_version_id=preimage.version_id,
                            preimage_digest=preimage.state_digest, create_intent_event_id=None)
        elif isinstance(preimage, c.CreateIntentRef):
            if (row.binding.space_id is not None
                    or preimage.operation_id != reservation.request.operation_id
                    or preimage.request_digest != reservation.request.request_digest
                    or not self._io(self.verify_create_intent, preimage)):
                raise CoordinationError('Committed provisional create intent required')
            evidence = dict(pre_version_id=None, preimage_digest=None,
                            create_intent_event_id=preimage.event_id)
        else:
            raise CoordinationError('Unsupported committed evidence')
        row = self._cas(row, lambda r: (r.state == c.CoordinationState.RESERVED
            and r.lease_expires_at > self.clock.now()
            and authorization.expires_at > self.clock.now()),
            state=c.CoordinationState.ADMITTED,
            active_operation_id=reservation.request.operation_id,
            idempotency_key=reservation.request.idempotency_key,
            request_digest=reservation.request.request_digest,
            approval_id=authorization.approval_id, approval_digest=authorization.approval_digest,
            expected_base_fingerprint=authorization.expected_base_fingerprints.state_digest,
            admitted_at=self.clock.now(), mutation_stage=c.PatchStage.CONFIG_PENDING,
            **evidence)
        return c.AdmissionClaim(row.binding.binding_id, row.binding.binding_revision,
            row.attempt_id, row.generation, row.row_version, reservation.request,
            preimage, row.approval_id, row.approval_digest)

    def _history(self, row, request):
        history = self._io(self.facts.lookup_request, row.binding, request.idempotency_key)
        if history.ambiguous:
            raise CoordinationError('Ambiguous durable request history')
        if any(f.request.request_digest != request.request_digest for f in history.facts):
            self._fact(row, request, c.FactStatus.CONFLICTED)
            raise CoordinationError('Idempotency key already bound to a different request digest')
        return history

    def _release(self, row):
        # Never erase heads or the monotonically increasing fence/observer sequence.
        return self._cas(row, lambda r: r.attempt_id == row.attempt_id,
            state=c.CoordinationState.IDLE, unresolved=False,
            holder=None, attempt_id=None, executor_kind=None, executor_ref=None,
            lease_expires_at=None, active_operation_id=None, idempotency_key=None,
            request_digest=None, approval_id=None, approval_digest=None,
            approval_consumption_published=False, pre_version_id=None, preimage_digest=None,
            create_intent_event_id=None, expected_base_fingerprint=None, admitted_at=None,
            mutation_stage=None, checkpoint=None, quarantine_reason=None, termination_evidence=None)

    def finish(self, claim: c.AdmissionClaim, result: c.OperationResult) -> None:
        self.assert_owner(claim)
        row = self._owned(claim)
        if (result.operation_id != row.active_operation_id or result.preimage != claim.preimage
                or result.unresolved or result.status not in {
                    c.OperationStatus.CONFIRMED, c.OperationStatus.NOOP,
                    c.OperationStatus.CONFLICTED, c.OperationStatus.FAILED}):
            raise CoordinationError('Not a resolved terminal result')
        if result.postimage is not None:
            if ((result.postimage.binding_id, result.postimage.binding_revision) !=
                    (row.binding.binding_id, row.binding.binding_revision)
                    or not self._io(self.ledger.verify_committed, result.postimage)):
                raise CoordinationError('Terminal observation not committed to this binding')
        elif result.status in {c.OperationStatus.CONFIRMED, c.OperationStatus.NOOP}:
            raise CoordinationError('Successful completion requires committed postimage')
        ref = self._fact(row, claim.request, c.FactStatus(result.status.value),
                         kind=c.FactKind.RECEIPT,
                         post_version_id=result.postimage.version_id if result.postimage else None)
        history = self._history(row, claim.request)
        if not any(f.event_id == ref.event_id for f in history.facts):
            raise AuthorityUnavailable('Terminal receipt publication not verified')
        self._release(row)
