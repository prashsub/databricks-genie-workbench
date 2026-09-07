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
                 authorize_heads=Mock(return_value=False),
                 validate_stage_evidence=Mock(return_value=True))
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


def durable_facts(h):
    def append(fact):
        h.durable.append(fact.event_key, c.to_wire(fact))
        return c.FactRef(fact.event_id, fact.event_key, 'd' * 64)

    def lookup(binding, key):
        rows = [c.from_wire(c.OperationFact, r.payload) for r in h.durable.state.rows]
        return c.RequestHistory(tuple(f for f in rows if f.binding == binding
                                      and f.request.idempotency_key == key), False)

    h.facts.append.side_effect = append
    h.facts.lookup_request.side_effect = lookup

    def publish(claim):
        evidence = c.StageEvidence('VC/1.0', c.PatchStage.CONFIG_PENDING, h.clock.now(),
                                   'd' * 64, claim.preimage if isinstance(claim.preimage, c.ObservationRef) else None)
        fact = c.OperationFact(uid(), c.FactKind.APPROVAL_CONSUMED,
            claim.request.operation_id, claim.row_version,
            f'consumption:{claim.attempt_id}', h.binding, claim.request, 'coordination',
            'target-service', c.ActorContext('target-service', h.binding.workspace_id, 'service'),
            c.FactStatus.CONSUMED, evidence, h.clock.now(), attempt_id=claim.attempt_id,
            generation=claim.generation, approval_id=claim.approval_id,
            approval_digest=claim.approval_digest)
        existing = h.durable.lookup(fact.event_key)
        if existing:
            old = c.from_wire(c.OperationFact, existing[0].payload)
            return c.FactRef(old.event_id, old.event_key, 'd' * 64)
        return append(fact)

    def use(binding, approval_id):
        rows = [c.from_wire(c.OperationFact, r.payload) for r in h.durable.state.rows]
        used = [f for f in rows if f.binding == binding and f.approval_id == approval_id
                and f.fact_kind == c.FactKind.APPROVAL_CONSUMED]
        return c.ApprovalUse(binding, approval_id,
            tuple(c.FactRef(f.event_id, f.event_key, 'd' * 64) for f in used),
            tuple(f.operation_id for f in used), False)

    h.facts.publish_consumption.side_effect = publish
    h.facts.approval_use.side_effect = use
    h.facts.verify_flush.side_effect = lambda claim: (claim.approval_id is None or
        claim.request.operation_id in use(h.binding, claim.approval_id).operation_ids)


def complete(h, claim):
    result = c.OperationResult(claim.request.operation_id, c.OperationStatus.CONFIRMED,
                               claim.preimage, h.preimage, False, ('verified',))
    h.service.finish(claim, result)
    return result


def test_same_key_different_digest_rejected_after_row_reuse(h):
    durable_facts(h)
    enroll(h)
    claim = admit(h)
    complete(h, claim)
    assert not h.store.read(h.binding.binding_id).unresolved
    # Reconstruct the service: all remembered authority must be in the stores.
    h.service = CoordinationService(**{k: getattr(h, k) for k in (
        'store', 'facts', 'ledger', 'clock', 'resolve_binding', 'verify_enrollment',
        'insert_enrolled', 'validate_authorization', 'verify_create_intent',
        'termination', 'authorize_human_recovery', 'authorize_heads')})
    h.request = replace(h.request, operation_id=uid(), request_digest='a' * 64)
    with pytest.raises(CoordinationError, match='digest'):
        reserve(h)
    assert h.store.read(h.binding.binding_id).state != c.CoordinationState.ADMITTED


def test_duplicate_completed_request_returns_receipt_without_admission(h):
    from backend.services.version_control.coordination import ExistingReceipt
    durable_facts(h)
    enroll(h)
    claim = admit(h)
    complete(h, claim)
    original_id = h.request.operation_id
    h.request = replace(h.request, operation_id=uid())
    before = h.store.read(h.binding.binding_id).generation
    with pytest.raises(ExistingReceipt) as caught:
        reserve(h)
    assert caught.value.receipt.operation_id == original_id
    assert caught.value.receipt.status == c.FactStatus.CONFIRMED
    row = h.store.read(h.binding.binding_id)
    assert row.generation == before + 1
    assert not row.unresolved and row.state == c.CoordinationState.IDLE


@pytest.mark.parametrize('crash', [None, 'before-commit', 'after-commit-before-response'])
def test_consumed_approval_survives_row_reuse_and_crash(h, crash):
    from backend.tests.vc_fakes.stores import CrashBoundary
    durable_facts(h)
    enroll(h)
    reservation = reserve(h)
    if crash:
        h.durable.failures.inject(CrashBoundary(crash))
        with pytest.raises(CoordinationError):
            h.service.admit(reservation, h.preimage, h.grant)
        row = h.store.read(h.binding.binding_id)
        assert row.unresolved and row.approval_id == h.grant.approval_id
        assert row.state == c.CoordinationState.QUARANTINED
        with pytest.raises(CoordinationError):
            reserve(h)
    else:
        claim = h.service.admit(reservation, h.preimage, h.grant)
        h.facts.publish_consumption.assert_called_once_with(claim)
        complete(h, claim)
        h.request = c.RequestIdentity(uid(), 'key-2', 'a' * 64)
        h.grant = replace(h.grant, request=h.request)
        with pytest.raises(CoordinationError, match='approval'):
            admit(h)
        assert h.facts.approval_use(h.binding, h.grant.approval_id).operation_ids == (claim.request.operation_id,)


@pytest.mark.parametrize('point', ['evidence', 'admission', 'consumption', 'intent', 'receipt', 'release'])
@pytest.mark.parametrize('boundary', ['before-commit', 'after-commit-before-response', 'during-follow-up-read'])
def test_crashes_between_evidence_cas_consumption_patch_and_release_remain_safe(h, point, boundary):
    from backend.tests.vc_fakes.stores import CrashBoundary
    durable_facts(h)
    enroll(h)
    reservation = reserve(h)
    if point == 'evidence':
        h.ledger.verify_committed.side_effect = OSError('Delta evidence unavailable')
        action = lambda: h.service.admit(reservation, h.preimage, h.grant)
    elif point in {'admission', 'consumption'}:
        failures = h.store.failures if point == 'admission' else h.durable.failures
        failures.inject(CrashBoundary(boundary))
        action = lambda: h.service.admit(reservation, h.preimage, h.grant)
    else:
        claim = h.service.admit(reservation, h.preimage, h.grant)
        if point == 'intent':
            h.store.failures.inject(CrashBoundary(boundary))
            action = lambda: h.service.checkpoint(claim, c.PatchStage.CONFIG_IN_FLIGHT,
                c.StageEvidence('VC/1.0', c.PatchStage.CONFIG_IN_FLIGHT, h.clock.now(), 'a' * 64, None))
        else:
            if point == 'receipt':
                if boundary == 'during-follow-up-read':
                    h.facts.lookup_request.side_effect = OSError('receipt read-back unavailable')
                else:
                    # Consumption already durable; inject only the next append (receipt).
                    h.durable.failures.inject(CrashBoundary(boundary))
            else:
                h.store.failures.inject(CrashBoundary(boundary))
            action = lambda: complete(h, claim)
    with pytest.raises(CoordinationError):
        action()
    row = h.store.read(h.binding.binding_id)
    if row.unresolved:
        assert row.attempt_id == reservation.fence.attempt_id
        with pytest.raises(CoordinationError):
            reserve(h)
    else:
        assert point == 'release'
        assert any(r.payload['fact_kind'] == 'receipt' for r in h.durable.state.rows)


def test_checkpoint_is_monotonic_and_matching_get_cannot_finish_in_flight(h):
    durable_facts(h)
    enroll(h)
    claim = admit(h)
    send = c.StageEvidence('VC/1.0', c.PatchStage.CONFIG_IN_FLIGHT, h.clock.now(), 'a' * 64, None)
    h.service.checkpoint(claim, send.stage, send)
    row = h.store.read(h.binding.binding_id)
    assert row.checkpoint['resume_classification'] == 'read-only-possible-send'
    with pytest.raises(CoordinationError):
        h.service.checkpoint(claim, send.stage, send)
    with pytest.raises(CoordinationError):
        complete(h, claim)
    assert h.store.read(h.binding.binding_id).state == c.CoordinationState.QUARANTINED


def test_checkpoint_observation_requires_trusted_send_completion(h):
    enroll(h)
    claim = admit(h)
    send = c.StageEvidence('VC/1.0', c.PatchStage.CONFIG_IN_FLIGHT, h.clock.now(), 'a' * 64, None)
    h.service.checkpoint(claim, send.stage, send)
    observed = c.StageEvidence('VC/1.0', c.PatchStage.CONFIG_OBSERVED, h.clock.now(), 'b' * 64, h.preimage)
    h.validate_stage_evidence.return_value = False  # GET equality alone
    with pytest.raises(CoordinationError):
        h.service.checkpoint(claim, observed.stage, observed)
    with pytest.raises(CoordinationError):
        h.service.checkpoint(claim, c.PatchStage.DESCRIPTION_PENDING,
            replace(observed, stage=c.PatchStage.DESCRIPTION_PENDING))


def test_checkpoint_normal_sequence_and_flush_failure_retains_claim(h):
    durable_facts(h)
    enroll(h)
    claim = admit(h)
    for stage in list(c.PatchStage)[1:]:
        observation = h.preimage if stage in {c.PatchStage.CONFIG_OBSERVED, c.PatchStage.DESCRIPTION_OBSERVED} else None
        h.service.checkpoint(claim, stage,
            c.StageEvidence('VC/1.0', stage, h.clock.now(), 'a' * 64, observation))
    h.facts.verify_flush.side_effect = lambda _: False
    with pytest.raises(CoordinationError):
        complete(h, claim)
    assert h.store.read(h.binding.binding_id).unresolved


def test_observer_cannot_regress_or_approve_heads(h):
    from backend.tests.vc_fakes.fixtures import FakeCanonicalizer
    enroll(h)
    lease = h.service.observe_exclusively(h.binding, h.executor)
    with pytest.raises(CoordinationError):
        reserve(h)
    with pytest.raises(CoordinationError):
        h.service.assert_owner(lease.fence)
    version_id = uid()
    for update in (c.HeadUpdate(version_id, version_id, None, 'caller-claim'),
                   c.HeadUpdate(version_id, None, version_id, 'caller-claim')):
        with pytest.raises(CoordinationError):
            h.service.advance_heads(lease.fence, update)
    snapshot = FakeCanonicalizer().observe({'serialized_space': {}, 'description': ''})
    context = c.CaptureContext(h.binding, 'observer-1', h.clock.now(), 'open',
        c.ActorContext('observer', h.binding.workspace_id, 'service'), c.Origin.EXTERNAL,
        attempt_id=lease.fence.attempt_id, generation=lease.fence.generation)
    h.ledger.get_version.return_value = c.Version(version_id, snapshot, context)
    heads = h.service.advance_heads(lease.fence, c.HeadUpdate(version_id, None, None, None))
    assert heads == c.Heads(version_id, None, None)
    assert not h.store.read(h.binding.binding_id).unresolved
    newer = h.service.observe_exclusively(h.binding, h.executor)
    assert newer.observed_sequence > lease.observed_sequence
    with pytest.raises(CoordinationError):
        h.service.advance_heads(lease.fence, c.HeadUpdate(uid(), None, None, None))
    # Even the current holder cannot attach an old observation from another lease.
    with pytest.raises(CoordinationError):
        h.service.advance_heads(newer.fence, c.HeadUpdate(version_id, None, None, None))
    final_id = uid()
    h.ledger.get_version.return_value = c.Version(final_id, snapshot,
        replace(context, observation_key='observer-2', parent_version_id=version_id,
                attempt_id=newer.fence.attempt_id, generation=newer.fence.generation))
    h.service.advance_heads(newer.fence, c.HeadUpdate(final_id, None, None, None))
    claim = admit(h)
    with pytest.raises(CoordinationError):
        h.service.advance_heads(claim, c.HeadUpdate(None, final_id, final_id, 'unvalidated'))
    h.authorize_heads.return_value = True
    heads = h.service.advance_heads(claim, c.HeadUpdate(None, final_id, final_id, 'verified-deployment-policy'))
    assert heads == c.Heads(final_id, final_id, final_id)
