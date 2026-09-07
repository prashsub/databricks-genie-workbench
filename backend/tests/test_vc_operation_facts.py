from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest

from backend.services.version_control.contracts import (
    FactKind, FactStatus, OperationFact, PatchStage, RequestIdentity, StageEvidence,
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
