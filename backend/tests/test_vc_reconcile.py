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


def test_reapply_requires_fresh_approval_for_new_base():
    dispatcher = Mock(spec=vc.JobDispatcher)
    dispatcher.submit_local.return_value = vc.OperationHandle(version_id(10), vc.OperationStatus.REQUESTED, "job/1")
    facts = Mock(spec=vc.OperationFacts)
    facts.approval_use.return_value = vc.ApprovalUse(binding_fixture(), version_id(19), (), (), False)
    service, observer, ledger, policy, approvals, identity = reconcile_setup(
        dispatcher=dispatcher, facts=facts, dispatch_enabled=True)
    captured = observed_result()
    observer.capture.return_value = replace(captured, status=replace(captured.status, drift=vc.DriftState.UNKNOWN))
    request = reconcile_request("reapply", approval_id=version_id(19))
    handle = service.reconcile(binding_fixture(), request, actor_fixture())
    inputs = approvals.request.call_args.args[0]
    assert inputs.source_version_id == version_id(1)
    assert inputs.expected_base_fingerprints == snapshot("external").fingerprints
    assert policy.inputs.call_args.args[1].approval_id is None
    invalidation = facts.append.call_args.args[0]
    assert invalidation.fact_kind == vc.FactKind.APPROVAL_INVALIDATED
    assert invalidation.approval_id == version_id(19)
    assert invalidation.pre_version_id == version_id(2)
    assert invalidation.status == vc.FactStatus.INVALIDATED
    dispatcher.submit_local.assert_called_once_with(version_id(10), "reconcile")
    assert handle.job_run_id == "job/1"
    approvals.authorize.assert_not_called()


def test_reapply_dispatch_is_default_off_and_capture_failure_never_dispatches():
    dispatcher = Mock(spec=vc.JobDispatcher)
    service, observer, ledger, policy, approvals, identity = reconcile_setup(dispatcher=dispatcher)
    with pytest.raises(PermissionError, match="dispatch"):
        service.reconcile(binding_fixture(), reconcile_request("reapply"), actor_fixture())
    observer.capture.assert_not_called()
    approvals.request.assert_not_called()
    dispatcher.submit_local.assert_not_called()
    service.dispatch_enabled = True
    observer.capture.side_effect = RuntimeError("capture failed")
    with pytest.raises(RuntimeError):
        service.reconcile(binding_fixture(), reconcile_request("reapply"), actor_fixture())
    dispatcher.submit_local.assert_not_called()


def test_reapply_invalidation_failure_stops_new_request_and_job():
    dispatcher = Mock(spec=vc.JobDispatcher)
    facts = Mock(spec=vc.OperationFacts)
    facts.approval_use.return_value = vc.ApprovalUse(binding_fixture(), version_id(19), (), (), False)
    facts.append.side_effect = RuntimeError("facts unavailable")
    service, observer, ledger, policy, approvals, identity = reconcile_setup(
        dispatcher=dispatcher, dispatch_enabled=True, facts=facts)
    with pytest.raises(RuntimeError):
        service.reconcile(binding_fixture(), reconcile_request("reapply", approval_id=version_id(19)), actor_fixture())
    approvals.request.assert_not_called()
    dispatcher.submit_local.assert_not_called()


def test_acknowledge_records_scoped_divergence_without_fabricating_clean_heads():
    facts = Mock(spec=vc.OperationFacts)
    service, observer, ledger, policy, approvals, identity = reconcile_setup(facts=facts)
    policy.authorize_acknowledgement.return_value = True
    request = reconcile_request("acknowledge", policy_inputs={"reason": "Accepted temporary divergence"})
    handle = service.reconcile(binding_fixture(), request, actor_fixture())
    fact = facts.append.call_args.args[0]
    assert fact.fact_kind == vc.FactKind.ACKNOWLEDGEMENT
    assert fact.status == vc.FactStatus.ACKNOWLEDGED
    assert fact.binding == binding_fixture()
    assert fact.pre_version_id == version_id(2)
    assert fact.evidence.source_version_id == version_id(1)
    assert fact.actor == actor_fixture()
    assert handle.status == vc.OperationStatus.CONFIRMED
    status = service.with_acknowledgement(observed_result().status, fact)
    assert status.heads == observed_result().status.heads
    assert status.drift == vc.DriftState.EXTERNAL_AHEAD
    assert "Acknowledged divergence" in status.reasons[-1]
    assert "acknowledge" not in status.allowed_actions
    approvals.request.assert_not_called()
    for changed in (replace(status, heads=replace(status.heads, observed=version_id(3))),
                    replace(status, heads=replace(status.heads, deployed=version_id(3))),
                    replace(status, binding_revision=2)):
        assert service.with_acknowledgement(changed, fact) == changed
    service.clock = lambda: NOW + timedelta(hours=2)
    assert service.with_acknowledgement(observed_result().status, fact) == observed_result().status


@pytest.mark.parametrize("failure", ["policy_denial", "missing_reason", "facts_unavailable"])
def test_acknowledgement_requires_policy_reason_and_durable_fact(failure):
    facts = Mock(spec=vc.OperationFacts)
    service, observer, ledger, policy, approvals, identity = reconcile_setup(facts=facts)
    policy.authorize_acknowledgement.return_value = failure != "policy_denial"
    request = reconcile_request("acknowledge", policy_inputs={} if failure == "missing_reason" else {"reason": "accepted"})
    if failure == "facts_unavailable":
        facts.append.side_effect = RuntimeError("unavailable")
    with pytest.raises((PermissionError, ValueError, RuntimeError)):
        service.reconcile(binding_fixture(), request, actor_fixture())
    approvals.request.assert_not_called()


def test_manual_merge_sql_changes_require_reviewed_new_payload():
    service, observer, ledger, policy, approvals, identity = reconcile_setup()
    service.canonicalizer = Mock(spec=vc.Canonicalizer)
    service.canonicalizer.compare.return_value = vc.Comparison.DIFFERENT
    service.canonicalizer.semantic_diff.return_value = [
        vc.DiffItem(vc.DiffCategory.SQL, "/instructions/sql", vc.DiffChange.MODIFIED,
                    "SELECT old", "SELECT new", False)]
    result = service.compare(binding_fixture(), version_id(1), version_id(2), actor_fixture())
    assert result.comparison == vc.Comparison.DIFFERENT
    assert result.items[0].review_required is True
    assert result.items[0].before == "SELECT old"
    observer.capture.assert_not_called()
    for payload in ({"automatic_merge": True}, {"merged_payload": {"sql": "SELECT merged"}},
                    {"serialized_space": {}}, {"sql": "SELECT new"}):
        with pytest.raises(ValueError, match="reviewed new payload"):
            service.reconcile(binding_fixture(), reconcile_request(policy_inputs=payload), actor_fixture())
    approvals.request.assert_not_called()
    ledger.append_observation.assert_not_called()


@pytest.mark.parametrize("failure", ["denied", "scope", "incompatible"])
def test_comparison_is_scoped_and_incompatible_is_unknown(failure):
    service, observer, ledger, policy, approvals, identity = reconcile_setup()
    if failure == "denied":
        identity.can_edit.return_value = False
    if failure == "scope":
        original = ledger.get_version.side_effect
        ledger.get_version.side_effect = lambda *args: replace(original(*args), context=replace(
            original(*args).context, binding=replace(binding_fixture(), space_id="other")))
    if failure == "incompatible":
        service.canonicalizer = Mock(spec=vc.Canonicalizer)
        service.canonicalizer.compare.return_value = vc.Comparison.UNKNOWN
        assert service.compare(binding_fixture(), version_id(1), version_id(2), actor_fixture()) == vc.SemanticDiff(vc.Comparison.UNKNOWN, ())
        service.canonicalizer.semantic_diff.assert_not_called()
    else:
        with pytest.raises(PermissionError):
            service.compare(binding_fixture(), version_id(1), version_id(2), actor_fixture())


def scan_setup(entries, **options):
    from backend.services.version_control.drift.ports import BindingInventory, Projections

    inventory = Mock(spec=BindingInventory)
    inventory.page.return_value = vc.Page(tuple(entries), "next")
    inventory.matches.side_effect = lambda binding: (binding,)
    projections = Mock(spec=Projections)
    dependencies = dict(inventory=inventory, projections=projections, scan_enabled=True,
                        batch_size=2, full_fetch_interval=timedelta(minutes=30), clock=lambda: NOW)
    dependencies.update(options)
    service, observer = setup_service(**dependencies)
    return service, observer, inventory, projections


def scan_entry(number=1, **changes):
    from backend.services.version_control.drift.ports import ScanEntry

    binding = replace(binding_fixture(), binding_id=version_id(number), space_id=f"space-{number}")
    status = observed_result(binding).status
    return replace(ScanEntry(binding, status, NOW, NOW, NOW - timedelta(hours=1)), **changes)


def test_reconcile_scan_paginates_isolates_failures_and_periodically_full_fetches():
    first, second = scan_entry(), scan_entry(2)
    service, observer, inventory, projections = scan_setup([first, second])
    observer.capture.side_effect = [RuntimeError("binding failed"), observed_result(second.binding)]
    batch = service.scan("123", "cursor")
    inventory.page.assert_called_once_with("123", "cursor", 2)
    assert batch.next_cursor == "next"
    assert len(batch.items) == 2
    assert batch.items[0].stale and batch.items[0].allowed_actions == ()
    assert batch.items[1].heads.observed == version_id(2)
    assert batch.items[1].drift == vc.DriftState.EXTERNAL_AHEAD
    assert observer.capture.call_count == 2
    assert projections.publish.call_count == 2
    fresh = replace(first, last_full_fetch_at=NOW)
    inventory.page.return_value = vc.Page((fresh,), None)
    observer.capture.reset_mock(side_effect=True)
    batch = service.scan("123", "next")
    observer.capture.assert_not_called()
    assert batch.next_cursor is None
    inventory.page.return_value = vc.Page((replace(fresh, api_update_time=NOW + timedelta(seconds=1)),), None)
    observer.capture.return_value = observed_result(first.binding)
    service.scan("123", None)
    observer.capture.assert_called_once()
    observer.capture.reset_mock()
    inventory.page.return_value = vc.Page((fresh,), None)
    service.clock = lambda: NOW + timedelta(minutes=31)
    service.scan("123", None)
    observer.capture.assert_called_once()


def test_scan_default_off_bounds_scope_and_job_executor():
    from backend.jobs.vc_reconcile import run_scan

    service, observer, inventory, projections = scan_setup([], scan_enabled=False)
    with pytest.raises(PermissionError):
        service.scan("123", None)
    inventory.page.assert_not_called()
    service.scan_enabled = True
    with pytest.raises(PermissionError):
        service.scan("other", None)
    inventory.page.return_value = vc.Page((scan_entry(), scan_entry(2), scan_entry(3)), None)
    with pytest.raises(ValueError, match="bound"):
        service.scan("123", None)
    observer.capture.assert_not_called()
    with pytest.raises(PermissionError):
        run_scan("123", None, service=service, executor=replace(executor_fixture(), execution_ref="app/1"))


def test_scan_projection_failure_isolated_without_advancing_full_fetch_watermark():
    first, second = scan_entry(), scan_entry(2)
    service, observer, inventory, projections = scan_setup([first, second])
    observer.capture.side_effect = [observed_result(first.binding), observed_result(second.binding)]
    projections.publish.side_effect = [RuntimeError("projection unavailable"), None]
    batch = service.scan("123", None)
    assert batch.items[0].stale
    assert batch.items[0].heads == observed_result(first.binding).status.heads
    assert not batch.items[1].stale
    assert observer.capture.call_count == 2
