"""M03 contract tests: durable fake stores, explicit clock, no Genie calls."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from backend.services.version_control import contracts as c
from backend.services.version_control.coordination import CoordinationService, CoordinationError
from backend.tests.vc_fakes.fixtures import binding_fixture, executor_fixture
from backend.tests.vc_fakes.stores import (
    CoordinationRow, FakeCoordinationStore, FakeFactStore, ManualClock,
)


def uid():
    return str(uuid4())


@pytest.fixture
def h():
    clock = ManualClock(datetime(2026, 9, 7, tzinfo=timezone.utc))
    binding = binding_fixture()
    store = FakeCoordinationStore(clock)
    durable = FakeFactStore(clock)
    facts = Mock(spec=c.OperationFacts)
    facts.lookup_request.return_value = c.RequestHistory((), False)
    facts.approval_use.side_effect = lambda b, a: c.ApprovalUse(b, a, (), (), False)
    facts.verify_flush.return_value = True
    ledger = Mock(spec=c.VersionLedger)
    ledger.verify_committed.return_value = True
    termination = Mock(spec=c.TerminationEvidenceProvider)
    termination.for_attempt.return_value = None
    ports = dict(store=store, facts=facts, ledger=ledger, clock=clock,
                 resolve_binding=Mock(return_value=binding),
                 verify_enrollment=Mock(return_value=True),
                 insert_enrolled=store.seed, validate_authorization=Mock(return_value=True),
                 verify_create_intent=Mock(return_value=True), termination=termination,
                 authorize_human_recovery=Mock(return_value=False),
                 authorize_heads=Mock(return_value=False))
    service = CoordinationService(**ports)
    proof = c.EnrollmentProof(uid(), binding.binding_id, 1, 'serialized-enrollment')
    request = c.RequestIdentity(uid(), 'key-1', '1' * 64)
    fp = c.Fingerprints('2' * 64, '3' * 64, '4' * 64, 'vc-c14n/1')
    preimage = c.ObservationRef(uid(), binding.binding_id, 1, fp.state_digest, '5' * 64)
    grant = c.AuthorizationGrant(binding, request, 'validated-policy', fp, '6' * 64,
                                 clock.now() + timedelta(hours=1), uid(), '7' * 64)
    return SimpleNamespace(**ports, service=service, binding=binding, proof=proof,
                           request=request, preimage=preimage, grant=grant,
                           executor=executor_fixture(), durable=durable)


def enroll(h):
    h.service.initialize(h.binding, h.proof)


def reserve(h):
    return h.service.reserve(h.binding, h.request, h.executor)


def admit(h):
    return h.service.admit(reserve(h), h.preimage, h.grant)


def test_missing_or_duplicate_ownership_row_blocks_admission(h):
    with pytest.raises(CoordinationError):
        reserve(h)
    assert not h.store.state.rows
    h.verify_enrollment.return_value = False
    with pytest.raises(CoordinationError):
        enroll(h)
    h.verify_enrollment.return_value = True
    enroll(h)
    enroll(h)  # idempotent enrollment does not INSERT twice
    assert len(h.store.state.rows) == 1
    h.resolve_binding.return_value = replace(h.binding, binding_revision=2)
    with pytest.raises(CoordinationError):
        reserve(h)
    h.resolve_binding.return_value = h.binding
    h.store.seed(CoordinationRow(h.binding, h.clock.now()))
    with pytest.raises(CoordinationError):
        reserve(h)


def test_two_concurrent_reservations_have_exactly_one_winner(h):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    enroll(h)
    barrier = Barrier(2)
    original = h.store.compare_and_swap
    trace = []

    def race(*args, **kwargs):
        barrier.wait(timeout=5)
        trace.append(kwargs)
        return original(*args, **kwargs)

    h.store.compare_and_swap = race

    def run():
        try:
            return reserve(h)
        except CoordinationError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    row = h.store.read(h.binding.binding_id)
    assert row.attempt_id == winners[0].fence.attempt_id
    assert row.holder == h.executor.principal_id
    assert row.generation == winners[0].fence.generation == 1
    assert row.unresolved and row.state == c.CoordinationState.RESERVED
    assert len(trace) == 2
    h.facts.append.assert_called_once()
    assert h.facts.append.call_args.args[0].status == c.FactStatus.CONFLICTED

    # Successful statement return is NOT proof: a non-committing adapter lies.
    other = replace(h.binding, binding_id=uid())
    h.resolve_binding.return_value = other
    h.store.seed(CoordinationRow(other, h.clock.now()))
    h.store.compare_and_swap = Mock(return_value=True)
    with pytest.raises(CoordinationError):
        h.service.reserve(other, h.request, h.executor)


@pytest.mark.parametrize('change', ['attempt', 'generation', 'revision'])
def test_same_sp_stale_attempt_cannot_renew_or_patch(h, change):
    enroll(h)
    reservation = reserve(h)
    renewed = h.service.renew(reservation.fence)
    assert renewed.row_version > reservation.fence.row_version
    row = h.store.read(h.binding.binding_id)
    changes = {'attempt_id': uid()} if change == 'attempt' else {'generation': row.generation + 1}
    if change == 'revision':
        changes = {'binding': replace(h.binding, binding_revision=2)}
        h.resolve_binding.return_value = changes['binding']
    h.store.state.rows[:] = [replace(row, **changes)]
    assert h.store.state.rows[0].holder == h.executor.principal_id
    with pytest.raises(CoordinationError):
        h.service.renew(renewed)
    with pytest.raises(CoordinationError):
        h.service.assert_owner(renewed)


@pytest.mark.parametrize('kind', ['observation', 'create_intent'])
def test_reservation_is_not_admission_and_requires_committed_preimage(h, kind):
    if kind == 'create_intent':
        h.binding = replace(h.binding, space_id=None)
        h.resolve_binding.return_value = h.binding
        h.grant = replace(h.grant, binding=h.binding)
        h.preimage = c.CreateIntentRef(uid(), h.request.operation_id, h.binding.binding_id,
                                       1, h.request.request_digest)
    enroll(h)
    reservation = reserve(h)
    assert not isinstance(reservation, c.AdmissionClaim)
    with pytest.raises(CoordinationError):
        h.service.assert_owner(reservation.fence)
    verifier = h.ledger.verify_committed if kind == 'observation' else h.verify_create_intent
    verifier.return_value = False
    with pytest.raises(CoordinationError):
        h.service.admit(reservation, h.preimage, h.grant)
    assert h.store.read(h.binding.binding_id).state == c.CoordinationState.RESERVED
    verifier.return_value = True
    claim = h.service.admit(reservation, h.preimage, h.grant)
    assert isinstance(claim, c.AdmissionClaim)
    assert claim.preimage == h.preimage
    h.service.assert_owner(claim)


def test_admission_atomically_binds_key_digest_approval_and_preimage(h):
    enroll(h)
    reservation = reserve(h)
    original = h.store.compare_and_swap
    h.store.compare_and_swap = Mock(wraps=original)
    h.validate_authorization.return_value = False
    with pytest.raises(CoordinationError):
        h.service.admit(reservation, h.preimage, h.grant)
    h.validate_authorization.return_value = True
    for bad in (replace(h.grant, request=replace(h.request, idempotency_key='other')),
                replace(h.grant, expires_at=h.clock.now()),
                replace(h.grant, binding=replace(h.binding, binding_revision=2)),
                replace(h.grant, approval_digest=None)):
        with pytest.raises(CoordinationError):
            h.service.admit(reservation, h.preimage, bad)
    assert h.store.compare_and_swap.call_count == 0
    claim = h.service.admit(reservation, h.preimage, h.grant)
    assert h.store.compare_and_swap.call_count == 1
    row = h.store.read(h.binding.binding_id)
    assert (row.active_operation_id, row.idempotency_key, row.request_digest) == (
        h.request.operation_id, h.request.idempotency_key, h.request.request_digest)
    assert (row.approval_id, row.approval_digest) == (h.grant.approval_id, h.grant.approval_digest)
    assert (row.pre_version_id, row.preimage_digest, row.expected_base_fingerprint) == (
        h.preimage.version_id, h.preimage.state_digest, h.grant.expected_base_fingerprints.state_digest)
    assert row.generation == claim.generation == reservation.fence.generation
    assert row.attempt_id == claim.attempt_id
    with pytest.raises(CoordinationError):
        h.service.assert_owner(replace(claim, request=replace(h.request, request_digest='a' * 64)))
