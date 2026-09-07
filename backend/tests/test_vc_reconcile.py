from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts as vc
from backend.tests.vc_fakes.fixtures import (
    FakeCanonicalizer, actor_fixture, binding_fixture, executor_fixture,
)
from backend.tests.test_vc_drift import lookup, snapshot, version_id


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def observed_result(binding=None):
    binding = binding or binding_fixture()
    summary = vc.VersionSummary(version_id(2), binding.binding_id, NOW, vc.Origin.EXTERNAL,
                                "unknown", version_id(1), None, snapshot("external").fingerprints,
                                None, None)
    status = vc.BindingStatus(binding.binding_id, binding.binding_revision,
                              vc.Heads(version_id(2), version_id(1), version_id(1)),
                              vc.DriftState.EXTERNAL_AHEAD, False, None, NOW, NOW, False,
                              ("compare", "adopt", "reapply", "acknowledge"), ("Policy differs",))
    return vc.ObservationResult(status, summary, False)


def setup_service(**options):
    from backend.services.version_control.drift import DriftService

    observer = Mock(spec=vc.Observer)
    observer.capture.return_value = observed_result()
    service = DriftService(canonicalizer=FakeCanonicalizer(), observer=observer,
                           executor=executor_fixture(), **options)
    return service, observer


def test_capture_advances_observed_but_drift_remains_until_policy_resolution():
    service, observer = setup_service()
    captured = service.prepare_choice(binding_fixture())
    observer.capture.assert_called_once_with(binding_fixture(), "reconcile", executor_fixture())
    assert captured.status.heads == vc.Heads(version_id(2), version_id(1), version_id(1))
    result = service.classify(captured.status.heads,
                             lookup({version_id(1): snapshot("policy"),
                                     version_id(2): snapshot("external")}), "reachable")
    assert result.state == vc.DriftState.EXTERNAL_AHEAD
    assert captured.captured_version.version_id == version_id(2)


@pytest.mark.parametrize("failure", ["busy", "stale", "unresolved", "quarantined", "wrong_binding"])
def test_capture_failure_never_offers_reconciliation(failure):
    service, observer = setup_service()
    captured = observed_result()
    if failure == "busy":
        captured = replace(captured, busy=True)
    else:
        changes = {"stale": {"stale": True}, "unresolved": {"unresolved_operation_id": version_id(7)},
                   "quarantined": {"quarantined": True}, "wrong_binding": {"binding_id": version_id(99)}}
        captured = replace(captured, status=replace(captured.status, **changes[failure]))
    observer.capture.return_value = captured
    with pytest.raises(RuntimeError):
        service.prepare_choice(binding_fixture())


def reconcile_request(action="adopt", **changes):
    request = vc.ReconcileRequest(vc.RequestIdentity(version_id(10), "reconcile-10", "a" * 64),
                                  action, 1, snapshot("external").state_digest, None, {})
    return replace(request, **changes)


def reconcile_setup(**options):
    from backend.services.version_control.drift.ports import ReconcilePolicy

    ledger = Mock(spec=vc.VersionLedger)
    def get_version(binding, reference):
        state = snapshot("external" if reference == version_id(2) else "policy")
        context = vc.CaptureContext(binding, reference, NOW, "reconcile", actor_fixture(), vc.Origin.EXTERNAL)
        return vc.Version(reference, state, context)
    ledger.get_version.side_effect = get_version
    policy = Mock(spec=ReconcilePolicy)
    def inputs(binding, request, source, base, actor):
        return vc.ApprovalInputs("VC/1.0", request.identity.operation_id, request.action,
                                 source.version_id, source.snapshot.raw_state_digest,
                                 source.snapshot.fingerprints, None, None, source.snapshot.state_digest,
                                 None, source.snapshot.fingerprints.canonicalizer_version, binding,
                                 base.snapshot.fingerprints, None, "b" * 64, "c" * 64, "d" * 64,
                                 {}, actor.subject_id, {}, NOW + timedelta(hours=1))
    policy.inputs.side_effect = inputs
    approvals = Mock(spec=vc.ApprovalService)
    approvals.request.side_effect = lambda inputs, actor: vc.ApprovalRequest(version_id(20), inputs, NOW)
    identity = Mock(spec=vc.IdentityProvider)
    identity.can_edit.return_value = True
    dependencies = dict(ledger=ledger, policy=policy, approvals=approvals, identity=identity,
                        reconcile_enabled=True, clock=lambda: NOW)
    dependencies.update(options)
    service, observer = setup_service(**dependencies)
    return service, observer, ledger, policy, approvals, identity


def test_adopt_creates_approval_request_not_automatic_approved_head():
    service, observer, ledger, policy, approvals, identity = reconcile_setup()
    events = []
    observer.capture.side_effect = lambda *args: events.append("capture") or observed_result()
    policy.inputs.side_effect = (lambda original: lambda *args: events.append("policy") or original(*args))(
        policy.inputs.side_effect)
    handle = service.reconcile(binding_fixture(), reconcile_request(), actor_fixture())
    assert events == ["capture", "policy"]
    inputs, actor = approvals.request.call_args.args
    assert inputs.source_version_id == version_id(2)
    assert inputs.expected_base_fingerprints == snapshot("external").fingerprints
    assert inputs.requester_id == actor_fixture().subject_id
    assert inputs.operation_type == "adopt"
    assert actor == actor_fixture()
    assert handle == vc.OperationHandle(version_id(10), vc.OperationStatus.REQUESTED, None)
    approvals.authorize.assert_not_called()
    ledger.append_observation.assert_not_called()
    assert observer.capture.return_value.status.heads.approved == version_id(1)


@pytest.mark.parametrize("failure", ["disabled", "denied", "workspace", "revision", "base", "policy_binding"])
def test_adopt_fails_closed_without_approval(failure):
    service, observer, ledger, policy, approvals, identity = reconcile_setup(
        reconcile_enabled=failure != "disabled")
    actor = actor_fixture()
    request = reconcile_request()
    if failure == "denied":
        identity.can_edit.return_value = False
    if failure == "workspace":
        actor = replace(actor, workspace_id="other")
    if failure == "revision":
        request = replace(request, binding_revision=2)
    if failure == "base":
        request = replace(request, expected_base="f" * 64)
    if failure == "policy_binding":
        original = policy.inputs.side_effect
        policy.inputs.side_effect = lambda *args: replace(original(*args), target_binding=replace(binding_fixture(), space_id="wrong"))
    with pytest.raises((PermissionError, ValueError, RuntimeError)):
        service.reconcile(binding_fixture(), request, actor)
    approvals.request.assert_not_called()
    if failure in ("disabled", "denied", "workspace", "revision"):
        observer.capture.assert_not_called()
