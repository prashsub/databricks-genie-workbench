"""Recovery never infers executor termination from a matching Genie GET."""
from dataclasses import replace
from datetime import timedelta

import pytest

from backend.services.version_control import contracts as c
from backend.services.version_control.coordination import CoordinationError
from backend.tests.test_vc_coordination import h, enroll, reserve, admit, durable_facts, uid


@pytest.mark.parametrize('entry', ['reserve', 'renew', 'assert_owner'])
def test_expired_lease_quarantines_without_takeover(h, entry):
    enroll(h)
    claim = admit(h)
    before = h.store.read(h.binding.binding_id)
    h.clock.advance(timedelta(seconds=61))
    with pytest.raises(CoordinationError):
        if entry == 'reserve':
            reserve(h)
        else:
            getattr(h.service, entry)(claim)
    row = h.store.read(h.binding.binding_id)
    assert row.state == c.CoordinationState.QUARANTINED
    assert row.unresolved
    assert (row.attempt_id, row.generation, row.approval_id, row.pre_version_id) == (
        before.attempt_id, before.generation, before.approval_id, before.pre_version_id)
    assert any(call.args[0].status == c.FactStatus.QUARANTINED for call in h.facts.append.call_args_list)
    with pytest.raises(CoordinationError):
        reserve(h)


def recovery_evidence(h, claim):
    terminal = h.clock.now()
    termination = c.TerminationEvidence(claim.attempt_id, h.executor.execution_ref,
                                        'app-worker', terminal, (), 'a' * 64)
    samples = tuple(c.RecoverySample(terminal + timedelta(seconds=i),
                                     h.preimage.state_digest, 'b' * 64) for i in (1, 12, 23))
    h.clock.advance(timedelta(seconds=30))
    return c.RecoveryEvidence('VC/1.0', claim, termination, samples, 20.0,
                              'verified-lifetime-reference', 'observed-preimage', None, None, False)


def test_matching_get_after_timeout_cannot_resolve_attempt(h):
    enroll(h)
    claim = admit(h)
    h.service.quarantine(claim, 'PATCH timeout; matching GET is observation only')
    evidence = recovery_evidence(h, claim)
    with pytest.raises(CoordinationError, match='termination'):
        h.service.recover(h.binding, evidence)
    with pytest.raises(CoordinationError):
        h.service.assert_owner(claim)
    with pytest.raises(CoordinationError):
        h.service.finish(claim, c.OperationResult(claim.request.operation_id,
            c.OperationStatus.CONFIRMED, claim.preimage, h.preimage, False, ('GET-matched',)))
    assert h.store.read(h.binding.binding_id).unresolved


@pytest.mark.parametrize('fault', [None, 'job-ok', 'wrong-attempt', 'missing-child',
    'no-worker-proof', 'two-reads', 'pre-termination', 'unstable', 'short-span', 'future'])
def test_recovery_needs_exact_attempt_termination_and_three_post_termination_reads(h, fault):
    durable_facts(h)
    enroll(h)
    claim = admit(h)
    h.service.quarantine(claim, 'unknown send outcome')
    evidence = recovery_evidence(h, claim)
    # Test-only verified capability. Production defaults to unavailable.
    h.service.verified_request_lifetime = (20.0, 'verified-lifetime-reference')
    trusted = evidence.termination
    if fault in {'job-ok', 'missing-child'}:
        executions = tuple(c.ExecutionTerminalEvidence(ref, trusted.terminated_at, 'c' * 64)
            for ref in (h.executor.execution_ref, 'job/retry', 'job/child'))
        trusted = replace(trusted, source='job', executions=executions, app_worker_proof_digest=None)
        evidence = replace(evidence, termination=trusted)
    h.termination.for_attempt.return_value = trusted
    if fault == 'wrong-attempt':
        evidence = replace(evidence, termination=replace(trusted, attempt_id=uid()))
    elif fault == 'missing-child':
        evidence = replace(evidence, termination=replace(trusted, executions=trusted.executions[:-1]))
    elif fault == 'no-worker-proof':
        evidence = replace(evidence, termination=replace(trusted, app_worker_proof_digest=None))
        h.termination.for_attempt.return_value = evidence.termination
    elif fault == 'two-reads':
        evidence = replace(evidence, samples=evidence.samples[:2])
    elif fault == 'pre-termination':
        evidence = replace(evidence, samples=(replace(evidence.samples[0], observed_at=trusted.terminated_at), *evidence.samples[1:]))
    elif fault == 'unstable':
        evidence = replace(evidence, samples=(*evidence.samples[:2], replace(evidence.samples[2], state_digest='c' * 64)))
    elif fault == 'short-span':
        evidence = replace(evidence, samples=(*evidence.samples[:2], replace(evidence.samples[2], observed_at=evidence.samples[0].observed_at + timedelta(seconds=20))))
    elif fault == 'future':
        evidence = replace(evidence, samples=(*evidence.samples[:2], replace(evidence.samples[2], observed_at=h.clock.now() + timedelta(seconds=1))))
    if fault in {None, 'job-ok'}:
        result = h.service.recover(h.binding, evidence)
        assert not result.unresolved
        assert result.fence.generation == claim.generation + 1
        assert h.store.read(h.binding.binding_id).state == c.CoordinationState.IDLE
        with pytest.raises(CoordinationError):
            h.service.assert_owner(claim)
        assert any(r.payload['fact_kind'] == 'recovery' for r in h.durable.state.rows)
    else:
        with pytest.raises(CoordinationError):
            h.service.recover(h.binding, evidence)
        assert h.store.read(h.binding.binding_id).unresolved


def test_recovery_persists_validated_termination_evidence_before_releasing(h):
    durable_facts(h)
    enroll(h)
    claim = admit(h)
    h.service.quarantine(claim, 'unknown send outcome')
    evidence = recovery_evidence(h, claim)
    h.service.verified_request_lifetime = (20.0, 'verified-lifetime-reference')
    h.termination.for_attempt.return_value = evidence.termination

    # 6. evidence is durable at the moment the recovery fact is appended.
    seen = {}
    real_append = h.facts.append.side_effect

    def capture(fact):
        if fact.fact_kind == c.FactKind.RECOVERY:
            seen['at_append'] = h.store.read(h.binding.binding_id).termination_evidence
        return real_append(fact)

    h.facts.append.side_effect = capture
    result = h.service.recover(h.binding, evidence)
    assert not result.unresolved
    assert c.to_wire(seen['at_append']) == c.to_wire(evidence.termination)
    # 9. released row clears the column; the durable RECOVERY fact persists.
    assert h.store.read(h.binding.binding_id).termination_evidence is None
    assert any(r.payload['fact_kind'] == 'recovery' for r in h.durable.state.rows)


def test_recovery_does_not_persist_invalid_termination(h):
    durable_facts(h)
    enroll(h)
    claim = admit(h)
    h.service.quarantine(claim, 'unknown send outcome')
    evidence = recovery_evidence(h, claim)
    h.service.verified_request_lifetime = (20.0, 'verified-lifetime-reference')
    h.termination.for_attempt.return_value = evidence.termination
    # 7. invalid termination never reaches the column.
    bad = replace(evidence, termination=replace(evidence.termination, attempt_id=uid()))
    with pytest.raises(CoordinationError):
        h.service.recover(h.binding, bad)
    assert h.store.read(h.binding.binding_id).termination_evidence is None


def test_recovery_crash_between_audit_and_release_keeps_termination_durable(h):
    durable_facts(h)
    enroll(h)
    claim = admit(h)
    h.service.quarantine(claim, 'unknown send outcome')
    evidence = recovery_evidence(h, claim)
    h.service.verified_request_lifetime = (20.0, 'verified-lifetime-reference')
    h.termination.for_attempt.return_value = evidence.termination
    # 8. crash between recovery append and release leaves evidence durable.
    real_append = h.facts.append.side_effect

    def append_then_break(fact):
        ref = real_append(fact)
        if fact.fact_kind == c.FactKind.RECOVERY:
            h.facts.lookup_request.side_effect = OSError('audit read-back unavailable')
        return ref

    h.facts.append.side_effect = append_then_break
    with pytest.raises(CoordinationError):
        h.service.recover(h.binding, evidence)
    row = h.store.read(h.binding.binding_id)
    assert row.state == c.CoordinationState.QUARANTINED
    assert c.to_wire(row.termination_evidence) == c.to_wire(evidence.termination)


@pytest.mark.parametrize('missing', [None, 'authorization', 'reason', 'risk', 'trusted-policy'])
def test_unknown_request_lifetime_disables_auto_clear(h, missing):
    durable_facts(h)
    enroll(h)
    claim = admit(h)
    h.service.quarantine(claim, 'worker disconnected')
    evidence = recovery_evidence(h, claim)
    h.termination.for_attempt.return_value = evidence.termination
    # Caller-supplied lifetime/reference cannot turn on automatic recovery.
    with pytest.raises(CoordinationError, match='unverified'):
        h.service.recover(h.binding, evidence)
    evidence = replace(evidence, maximum_request_lifetime_seconds=None,
        lifetime_bound_reference=None, human_authorization_reference='audited-operator/123',
        human_reason='Orphan inspection complete; reconcile under new governed request',
        residual_risk_acknowledged=True)
    h.authorize_human_recovery.return_value = True
    if missing == 'authorization':
        evidence = replace(evidence, human_authorization_reference=None)
    elif missing == 'reason':
        evidence = replace(evidence, human_reason='')
    elif missing == 'risk':
        evidence = replace(evidence, residual_risk_acknowledged=False)
    elif missing == 'trusted-policy':
        h.authorize_human_recovery.return_value = False
    if missing is None:
        result = h.service.recover(h.binding, evidence)
        assert not result.unresolved
        audits = [r for r in h.durable.state.rows if r.payload['fact_kind'] == 'recovery']
        assert audits[-1].payload['evidence']['residual_risk_acknowledged'] is True
        assert audits[-1].payload['evidence']['human_authorization_reference'] == 'audited-operator/123'
    else:
        with pytest.raises(CoordinationError):
            h.service.recover(h.binding, evidence)
        assert h.store.read(h.binding.binding_id).unresolved
