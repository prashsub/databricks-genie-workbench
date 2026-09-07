"""Synchronous, fail-closed coordination. Never sends a Genie mutation."""
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

    def reserve(self, binding, operation, executor):
        self._row(binding)
        raise CoordinationError('Reservation not yet available')
