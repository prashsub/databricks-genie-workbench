"""Mock-gap closure: M03/M04 fact writers validated by the REAL M06 durable ledger.

Every prior coordination/gate test injected a mock (or the hand-rolled ``durable_facts``
double, which skips ``validate_evidence`` and the ``event_key == fact_key`` check) for the
``OperationFacts`` port. As a result non-deterministic event keys and ``RECEIPT`` facts
carrying ``StageEvidence`` passed offline yet failed on the live ``DurableOperationFacts``
validator — the D2.6 promotion-gate failure. These tests wire the writers through the real
validator so that class of drift cannot regress silently.
"""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.coordination.service import (
    _SEQ_CLOSURE,
    _SEQ_QUARANTINE,
    _SEQ_RECOVERY,
    _SEQ_REJECT,
)
from backend.services.version_control.governance.facts import (
    DurableOperationFacts,
    validate_evidence,
)
from backend.services.version_control.mutation_gate import MutationGate
from backend.tests.test_vc_coordination import enroll, h, reserve, uid  # noqa: F401
from backend.tests.test_vc_mutation_gate import (
    rig,  # noqa: F401  (reused pytest fixture)
)
from backend.tests.vc_fakes.fixtures import binding_fixture, executor_fixture
from backend.tests.vc_fakes.stores import FakeFactStore, ManualClock

NOW = datetime(2026, 9, 7, tzinfo=UTC)

# Coordination ports the real validator swap has to preserve when rebuilding the service.
_COORD_PORTS = (
    'store', 'ledger', 'clock', 'resolve_binding', 'verify_enrollment', 'insert_enrolled',
    'validate_authorization', 'verify_create_intent', 'termination',
    'authorize_human_recovery', 'authorize_heads', 'validate_stage_evidence')


def _durable(clock):
    store = FakeFactStore(clock)
    facts = DurableOperationFacts(
        lambda: tuple(row.payload for row in store.state.rows),
        store.append, writes_enabled=True)
    return facts, store


def _rows(store):
    return [vc.from_wire(vc.OperationFact, row.payload) for row in store.state.rows]


def _assert_deterministic(fact):
    assert fact.event_key == vc.fact_key(fact)
    assert fact.event_id == vc.deterministic_event_id(fact.event_key)


# --------------------------------------------------------------------------- gate (M04)

def test_gate_terminal_operation_fact_conforms_to_durable_validator(rig):
    """A happy-path edit's terminal OPERATION fact is accepted by the real ledger."""
    facts, store = _durable(ManualClock(NOW))
    gate = MutationGate(coordination=rig.coordination, ledger=rig.ledger, facts=facts,
                        approvals=rig.approvals, transport=rig.transport,
                        canonicalizer=rig.canonicalizer, flags=rig.flags)

    result = gate.execute(rig.request, rig.executor)

    assert result.status == vc.OperationStatus.CONFIRMED
    rows = _rows(store)
    terminal = [f for f in rows if f.fact_kind == vc.FactKind.OPERATION
                and f.transition_sequence == 100]
    assert len(terminal) == 1 and terminal[0].status == vc.FactStatus.CONFIRMED
    _assert_deterministic(terminal[0])
    # RECEIPT is reserved for the target-written DeploymentReceipt; the gate authors none.
    assert not any(f.fact_kind == vc.FactKind.RECEIPT for f in rows)


def test_gate_create_intent_fact_conforms_to_durable_validator():
    """The CREATE_INTENT fact the gate assembles satisfies the strict create-intent rules
    (evidence is a CreateIntentRef whose event_id equals the sealed fact identity)."""
    facts, store = _durable(ManualClock(NOW))
    binding = replace(binding_fixture(), space_id=None)  # provisional: create has no space yet
    executor = executor_fixture()
    identity = vc.RequestIdentity(uid(), 'create-key', 'a' * 64)
    actor = vc.ActorContext(executor.principal_id, executor.workspace_id, executor.actor_kind)
    # Mirror MutationGate.create's construction exactly.
    draft = vc.OperationFact(identity.operation_id, vc.FactKind.CREATE_INTENT,
        identity.operation_id, 0, '', binding, identity, 'create', executor.principal_id, actor,
        vc.FactStatus.REQUESTED,
        vc.CreateIntentRef(identity.operation_id, identity.operation_id, binding.binding_id,
            binding.binding_revision, identity.request_digest), NOW)
    sealed = vc.seal_fact(draft)
    intent = vc.CreateIntentRef(sealed.event_id, identity.operation_id, binding.binding_id,
        binding.binding_revision, identity.request_digest)

    reference = facts.append(replace(sealed, evidence=intent))

    committed = _rows(store)
    assert len(committed) == 1 and committed[0].fact_kind == vc.FactKind.CREATE_INTENT
    assert committed[0].evidence == intent and reference.event_id == committed[0].event_id
    _assert_deterministic(committed[0])


# ------------------------------------------------------------------- coordination (M03)

def _real_facts_service(h):
    facts, store = _durable(h.clock)
    h.service = h.service.__class__(facts=facts, **{k: getattr(h, k) for k in _COORD_PORTS})
    h.facts = facts
    return store


def test_coordination_fact_variants_conform_and_coexist(h):
    """Every coordination-authored fact (reject / quarantine / closure / recovery) passes
    the real validator, and the distinct transition-sequence categories keep them in
    separate durable slots — including alongside the gate's terminal OPERATION@100 for the
    same (operation, attempt, generation)."""
    store = _real_facts_service(h)
    enroll(h)
    reservation = reserve(h)
    row = h.store.read(h.binding.binding_id)
    req = h.request

    reject = h.service._fact(row, req, vc.FactStatus.CONFLICTED, sequence=_SEQ_REJECT)
    quarantine = h.service._fact(row, req, vc.FactStatus.QUARANTINED, sequence=_SEQ_QUARANTINE)
    closure = h.service._fact(row, req, vc.FactStatus.CONFIRMED, sequence=_SEQ_CLOSURE,
                              post_version_id=h.preimage.version_id)
    termination = vc.TerminationEvidence(reservation.fence.attempt_id, h.executor.execution_ref,
                                         'job', h.clock.now(), (), None)
    rec_ev = vc.RecoveryEvidence('VC/1.0', reservation.fence, termination, (), None, None,
                                 'read-only-presend', None, None, False)
    recovery = h.service._fact(row, req, vc.FactStatus.CONFLICTED, evidence=rec_ev,
                               kind=vc.FactKind.RECOVERY, sequence=_SEQ_RECOVERY)

    rows = _rows(store)
    refs = {reject.event_id, quarantine.event_id, closure.event_id, recovery.event_id}
    assert len(refs) == 4  # every category minted a distinct durable fact
    assert len({f.event_key for f in rows}) == len(rows)  # no key collisions
    for fact in rows:
        _assert_deterministic(fact)
        validate_evidence(fact)  # the validator would have raised inside append otherwise
    # Coordination's closure is an OPERATION fact, never FactKind.RECEIPT.
    assert not any(f.fact_kind == vc.FactKind.RECEIPT for f in rows)
    assert any(f.fact_kind == vc.FactKind.OPERATION and f.operation_type == 'coordination'
               and f.transition_sequence == _SEQ_CLOSURE for f in rows)

    # The gate's terminal OPERATION@100 for the SAME attempt coexists (distinct sequence
    # => distinct key), so finish's dual write (gate terminal + coordination closure) is safe.
    gate_terminal = vc.seal_fact(vc.OperationFact(req.operation_id, vc.FactKind.OPERATION,
        req.operation_id, 100, '', h.binding, req, 'edit', h.executor.principal_id,
        vc.ActorContext(h.executor.principal_id, h.executor.workspace_id, h.executor.actor_kind),
        vc.FactStatus.CONFIRMED,
        vc.StageEvidence('VC/1.0', vc.PatchStage.CONFIG_OBSERVED, h.clock.now(),
                         h.preimage.state_digest, None), h.clock.now(),
        attempt_id=reservation.fence.attempt_id, generation=reservation.fence.generation,
        post_version_id=h.preimage.version_id))
    h.facts.append(gate_terminal)  # must not raise despite the closure sharing op/attempt/gen
    assert gate_terminal.event_key != closure.event_id  # different slot from the closure


def test_receipt_kind_rejects_stage_evidence(h):
    """Locks the reconciliation invariant: RECEIPT carries a DeploymentReceipt, so the
    receipt-equivalent coordination terminal MUST be an OPERATION fact, not a RECEIPT with
    StageEvidence (which is exactly what earlier drafts wrote and the live ledger rejected)."""
    store = _real_facts_service(h)
    enroll(h)
    reserve(h)
    row = h.store.read(h.binding.binding_id)
    receipt_stage = vc.seal_fact(vc.OperationFact(h.request.operation_id, vc.FactKind.RECEIPT,
        h.request.operation_id, _SEQ_CLOSURE, '', h.binding, h.request, 'coordination',
        'coordination', vc.ActorContext('coordination', h.binding.workspace_id, 'service'),
        vc.FactStatus.CONFIRMED,
        vc.StageEvidence('VC/1.0', vc.PatchStage.CONFIG_OBSERVED, h.clock.now(),
                         h.preimage.state_digest, None), h.clock.now(),
        attempt_id=row.attempt_id, generation=row.generation))

    with pytest.raises(ValueError, match='typed evidence'):
        h.facts.append(receipt_stage)
    assert not store.state.rows
