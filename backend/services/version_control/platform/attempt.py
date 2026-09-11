"""Durable attempt-state classifier for the governed job runtime.

`GovernedJobRuntime.run` consults `attempt_state(operation_id)` before it lets a
mutation handler run: only a `"new"` attempt executes; `"unresolved"` and
`"terminal"` are routed to verify-only reconstruction (no second mutation). The
classification is derived exclusively from committed operation facts and reuses
`MutationGate._requires_verify_only` as the single source of truth for whether a
prior attempt already touched the target — the same predicate the gate itself
applies on the write path, so the job runtime and the gate can never disagree.
"""

from collections.abc import Callable

from backend.services.version_control.contracts import FactKind, FactStatus
from backend.services.version_control.mutation_gate import MutationGate

# A settled outcome: nothing further to reconcile, verify-only just re-observes.
_TERMINAL_STATUSES = frozenset({
    FactStatus.CONFIRMED,
    FactStatus.NOOP,
    FactStatus.COMPENSATED,
    FactStatus.FAILED,
})


def build_attempt_state(facts) -> Callable[[str], str]:
    """Return an `attempt_state(operation_id) -> {"new","unresolved","terminal"}`
    closure bound to a durable `OperationFacts` port.

    - `"new"`: no prior attempt requires verify-only — the handler may run.
    - `"terminal"`: the latest operation fact is a settled outcome.
    - `"unresolved"`: an attempt is in flight / applied-unverified / conflicted /
      quarantined, or the durable history is ambiguous — never re-mutate.
    """

    def attempt_state(operation_id: str) -> str:
        approved = facts.get_request(operation_id)
        request = approved.request
        history = facts.lookup_request(request.binding, request.identity.idempotency_key)
        if history.ambiguous:
            return "unresolved"
        scoped = [fact for fact in history.facts if fact.operation_id == operation_id]
        if not any(MutationGate._requires_verify_only(fact) for fact in scoped):
            return "new"
        operations = [fact for fact in scoped if fact.fact_kind == FactKind.OPERATION]
        latest = max(operations, key=lambda fact: fact.transition_sequence)
        return "terminal" if latest.status in _TERMINAL_STATUSES else "unresolved"

    return attempt_state
