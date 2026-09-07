from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest

from backend.services.version_control.contracts import (
    AdmissionClaim, CreateIntentRef, FactKind, FactStatus, MutationRequest,
    ObservationRef, OperationFact, OperationFacts, PatchStage, RequestIdentity, StageEvidence,
    to_wire,
)
from backend.tests.vc_fakes.fixtures import actor_fixture, binding_fixture
from backend.tests.vc_fakes.stores import (
    CrashBoundary, FakeFactStore, InjectedCrash, ManualClock,
)


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)
DIGEST = 'a' * 64


def uid(number):
    return str(UUID(int=number))


def fact(**changes):
    from backend.services.version_control.governance.facts import fact_key

    values = dict(
        event_id=uid(1), fact_kind=FactKind.OPERATION, operation_id=uid(2),
        transition_sequence=0, event_key='', binding=binding_fixture(),
        request=RequestIdentity(uid(2), 'client-key', DIGEST), operation_type='edit',
        requester_id=actor_fixture().subject_id, actor=actor_fixture(),
        status=FactStatus.REQUESTED,
        evidence=StageEvidence('VC/1.0', PatchStage.CONFIG_PENDING, NOW, DIGEST, None),
        recorded_at=NOW,
    )
    values.update(changes)
    provisional = OperationFact(**values)
    key = fact_key(provisional)
    return replace(provisional, event_key=key, event_id=str(uuid5(NAMESPACE_URL, key)))


def durable(store=None, enabled=True):
    from backend.services.version_control.governance.facts import DurableOperationFacts

    store = store or FakeFactStore(ManualClock(NOW))

    def read():
        store.failures.hit(CrashBoundary.DURING_FOLLOW_UP_READ)
        return tuple(row.payload for row in store.state.rows)

    return DurableOperationFacts(read, store.append, writes_enabled=enabled), store


def test_operation_fact_schema_and_idempotent_append_survive_response_loss():
    ledger, store = durable()
    original = fact()
    store.failures.inject(CrashBoundary.AFTER_COMMIT_BEFORE_RESPONSE)
    with pytest.raises(InjectedCrash):
        ledger.append(original)
    restarted, _ = durable(store)
    reference = restarted.append(original)
    assert reference.event_key == original.event_key
    assert len(store.state.rows) == 1
    with pytest.raises(ValueError, match='event key'):
        restarted.append(replace(original, event_key='caller-controlled'))
    with pytest.raises(ValueError, match='identity'):
        restarted.append(fact(operation_id=uid(99)))
    with pytest.raises(ValueError, match='conflict'):
        restarted.append(replace(original, status=FactStatus.CONFIRMED))
    store.failures.inject(CrashBoundary.DURING_FOLLOW_UP_READ)
    with pytest.raises(InjectedCrash):
        restarted.append(original)
    assert len(store.state.rows) == 1


def test_fact_writes_default_off():
    from backend.services.version_control.governance.facts import DurableOperationFacts

    ledger = DurableOperationFacts(lambda: (), lambda *args: pytest.fail('write'))
    with pytest.raises(PermissionError, match='disabled'):
        ledger.append(fact())


def test_completed_request_and_consumed_approval_remain_after_coordination_reuse():
    ledger, store = durable()
    initial = fact()
    request = MutationRequest(initial.request, initial.binding, 'edit', uid(3),
                              DIGEST, {'instructions': []}, None, uid(4))
    ledger.record_request(request, initial)
    consumed = fact(fact_kind=FactKind.APPROVAL_CONSUMED, status=FactStatus.CONSUMED,
                    approval_id=uid(4), approval_digest=DIGEST,
                    attempt_id=uid(5), generation=1)
    ledger.append(consumed)
    ledger.append(fact(transition_sequence=1, status=FactStatus.CONFIRMED))
    ledger.append(fact(operation_id=uid(6), request=RequestIdentity(uid(6), 'later', DIGEST)))
    restarted, _ = durable(store)
    history = restarted.lookup_request(initial.binding, 'client-key')
    assert not history.ambiguous
    assert any(row.status == FactStatus.CONFIRMED for row in history.facts)
    assert restarted.get_request(uid(2)).request == request
    use = restarted.approval_use(initial.binding, uid(4))
    assert use.operation_ids == (uid(2),)
    assert len(use.consumptions) == 1
    with pytest.raises(ValueError, match='idempotency'):
        restarted.append(fact(operation_id=uid(7), request=RequestIdentity(uid(7), 'client-key', 'b' * 64)))
    store.seed(replace(store.state.rows[0], payload=to_wire(fact(
        operation_id=uid(8), request=RequestIdentity(uid(8), 'client-key', 'b' * 64)))))
    assert restarted.lookup_request(initial.binding, 'client-key').ambiguous


def test_create_intent_is_durable_without_fake_pre_version():
    ledger, store = durable()
    binding = replace(binding_fixture(), space_id=None)
    provisional = fact(binding=binding, fact_kind=FactKind.CREATE_INTENT, operation_type='create')
    intent = CreateIntentRef(provisional.event_id, uid(2), binding.binding_id, 1, DIGEST)
    created = replace(provisional, evidence=intent)
    ledger.append(created)
    restarted, _ = durable(store)
    assert restarted.lookup_request(binding, 'client-key').facts == (created,)
    assert created.pre_version_id is None
    with pytest.raises(ValueError, match='create intent'):
        durable()[0].append(replace(created, pre_version_id=uid(10)))
    with pytest.raises(ValueError, match='create intent'):
        durable()[0].append(replace(created, evidence=fact().evidence))
    with pytest.raises(ValueError, match='create intent'):
        durable()[0].append(replace(created, evidence=replace(intent, binding_revision=2)))


class ClaimConsumer:
    def __init__(self, facts: OperationFacts, claim):
        self.facts = facts
        self.claim = claim
        self.unresolved = True

    def finish(self):
        self.facts.publish_consumption(self.claim)
        if not self.facts.verify_flush(self.claim):
            raise PermissionError('Unflushed claim must remain unresolved')
        self.unresolved = False


@pytest.mark.parametrize('boundary', list(CrashBoundary))
def test_consumption_publish_failure_retains_unresolved_claim(boundary):
    from backend.tests.test_vc_approvals import approve, setup_approval

    context = setup_approval()
    approve(context)
    grant = context.service.authorize(context.request, context.executor)
    observation = ObservationRef(uid(3), grant.binding.binding_id, 1,
                                 context.request.expected_base, DIGEST)
    claim = AdmissionClaim(grant.binding.binding_id, 1, uid(5), 1, 2,
                           grant.request, observation, grant.approval_id, grant.approval_digest)
    consumer = ClaimConsumer(context.facts, claim)
    assert not context.facts.verify_flush(claim)
    context.store.failures.inject(boundary)
    with pytest.raises(InjectedCrash):
        consumer.finish()
    assert consumer.unresolved
    restarted, _ = durable(context.store)
    consumer.facts = restarted
    consumer.finish()
    assert not consumer.unresolved
    assert restarted.verify_flush(claim)
    assert len(restarted.approval_use(grant.binding, grant.approval_id).consumptions) == 1
    assert not restarted.verify_flush(replace(claim, generation=2))
    with pytest.raises((PermissionError, ValueError)):
        restarted.publish_consumption(replace(claim, attempt_id=uid(55), generation=2))
    context.service.facts = restarted
    with pytest.raises(PermissionError, match='consumed'):
        context.service.authorize(context.request, context.executor)
