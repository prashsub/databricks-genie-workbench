"""A5: durable attempt-state classifier (offline, fake facts)."""

from types import SimpleNamespace

from backend.services.version_control.contracts import FactKind, FactStatus
from backend.services.version_control.platform.attempt import build_attempt_state

OP = "00000000-0000-4000-8000-000000000011"


def _fact(status, seq, *, kind=FactKind.OPERATION, operation_id=OP):
    return SimpleNamespace(operation_id=operation_id, fact_kind=kind, status=status,
                           transition_sequence=seq, attempt_id="a", evidence=None)


def _facts(history_facts, *, ambiguous=False):
    binding = SimpleNamespace(binding_id="b", binding_revision=1, workspace_id="target")
    identity = SimpleNamespace(idempotency_key="idem")
    request = SimpleNamespace(binding=binding, identity=identity)
    port = SimpleNamespace()
    port.get_request = lambda _op: SimpleNamespace(request=request)
    port.lookup_request = lambda _b, _k: SimpleNamespace(
        facts=tuple(history_facts), ambiguous=ambiguous)
    return port


def test_new_when_no_facts():
    assert build_attempt_state(_facts([]))(OP) == "new"


def test_new_ignores_non_operation_facts():
    granted = _fact(FactStatus.APPROVED, 1, kind=FactKind.APPROVAL_GRANTED)
    assert build_attempt_state(_facts([granted]))(OP) == "new"


def test_new_when_operation_facts_belong_to_other_operation():
    other = _fact(FactStatus.CONFIRMED, 5, operation_id="other-op")
    assert build_attempt_state(_facts([other]))(OP) == "new"


def test_terminal_on_confirmed():
    facts = _facts([_fact(FactStatus.PREIMAGE_CAPTURED, 1), _fact(FactStatus.CONFIRMED, 2)])
    assert build_attempt_state(facts)(OP) == "terminal"


def test_terminal_on_noop():
    assert build_attempt_state(_facts([_fact(FactStatus.NOOP, 1)]))(OP) == "terminal"


def test_unresolved_on_applied_unverified():
    facts = _facts([_fact(FactStatus.APPLIED_UNVERIFIED, 1)])
    assert build_attempt_state(facts)(OP) == "unresolved"


def test_unresolved_on_applied_partial():
    facts = _facts([_fact(FactStatus.APPLIED_PARTIAL, 1)])
    assert build_attempt_state(facts)(OP) == "unresolved"


def test_latest_transition_decides():
    # A later applied-partial supersedes an earlier confirmed transition.
    facts = _facts([_fact(FactStatus.CONFIRMED, 1), _fact(FactStatus.APPLIED_PARTIAL, 2)])
    assert build_attempt_state(facts)(OP) == "unresolved"


def test_unresolved_when_history_ambiguous():
    facts = _facts([_fact(FactStatus.CONFIRMED, 1)], ambiguous=True)
    assert build_attempt_state(facts)(OP) == "unresolved"
