"""M04 Job restore and conditional compensation port tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.restore import RestoreService
from backend.tests.test_vc_mutation_gate import rig, uid
from backend.tests.vc_fakes.fixtures import actor_fixture


@pytest.fixture
def restore_rig(rig):
    source = vc.Version(rig.request.source_version_id, rig.desired,
        vc.CaptureContext(rig.binding, "historical", datetime.now(timezone.utc),
                          "history", actor_fixture(), vc.Origin.WORKBENCH))
    rig.versions[source.version_id] = source
    request = replace(rig.request, operation_type="restore")
    rig.facts.get_request.return_value = vc.ApprovedOperation(request, None)
    dispatcher = Mock(spec=vc.JobDispatcher)
    dispatcher.submit_local.return_value = vc.OperationHandle(request.identity.operation_id,
                                                             vc.OperationStatus.REQUESTED, "run/123")
    identity = Mock(spec=vc.IdentityProvider)
    identity.can_edit.return_value = True
    validate = Mock(return_value=True)
    service = RestoreService(facts=rig.facts, ledger=rig.ledger, gate=rig.gate,
        dispatcher=dispatcher, identity=identity, flags=rig.flags, validate=validate)
    return rig, service, request, dispatcher, identity, validate


def test_restore_is_job_executed_and_appends_historical_lineage(restore_rig):
    rig, service, request, dispatcher, identity, validate = restore_rig
    handle = service.submit(request.identity.operation_id, actor_fixture())
    assert handle is not None, "Restore must dispatch an operation-id-only Job"
    dispatcher.submit_local.assert_called_once_with(request.identity.operation_id, "restore")
    rig.transport.patch_config_once.assert_not_called()
    result = service.run(request.identity.operation_id, rig.executor)
    assert result.status == vc.OperationStatus.CONFIRMED
    version = rig.versions[result.postimage.version_id]
    assert version.context.restored_from_version_id == request.source_version_id
    assert version.context.parent_version_id == result.preimage.version_id
    assert version.context.origin == vc.Origin.RESTORE
    identity.verify_run_as.assert_called_once_with(rig.executor.execution_ref, rig.executor.principal_id)
    assert validate.call_count == 2


@pytest.mark.parametrize("invalid", ["workspace", "app", "source", "validation"])
def test_restore_rejects_invalid_job_or_historical_payload(restore_rig, invalid):
    rig, service, request, dispatcher, identity, validate = restore_rig
    executor = rig.executor
    if invalid == "workspace":
        executor = replace(executor, workspace_id="other")
    elif invalid == "app":
        executor = replace(executor, execution_ref="app/worker")
    elif invalid == "source":
        rig.facts.get_request.return_value = vc.ApprovedOperation(replace(request, serialized_space={"tampered": True}), None)
    else:
        validate.return_value = False
    with pytest.raises((PermissionError, ValueError)):
        service.run(request.identity.operation_id, executor)
    rig.transport.patch_config_once.assert_not_called()


@pytest.mark.parametrize("case", ["authorized", "no_policy", "external_drift", "ambiguous", "stale_post_stage"])
def test_compensation_requires_preauthorization_and_unchanged_post_stage(restore_rig, case):
    rig, service, original, dispatcher, identity, validate = restore_rig
    applied = service.run(original.identity.operation_id, rig.executor)
    original_fact = rig.facts.append.call_args.args[0]
    compensation = replace(original, identity=vc.RequestIdentity(uid(), "compensate", "b" * 64),
        source_version_id=applied.preimage.version_id, expected_base=applied.postimage.state_digest,
        serialized_space=rig.before.serialized_space, description="old")
    if case == "stale_post_stage":
        compensation = replace(compensation, expected_base=applied.preimage.state_digest)
    approval = Mock(spec=vc.ApprovalRecord)
    approval.request = Mock(spec=vc.ApprovalRequest)
    approval.request.requested_at = datetime.now(timezone.utc) - timedelta(hours=1)
    approval.request.inputs = Mock(spec=vc.ApprovalInputs)
    approval.request.inputs.recovery_policy = {} if case == "no_policy" else {
        "compensation_operation_id": compensation.identity.operation_id,
        "compensation_request_digest": compensation.identity.request_digest}
    rig.facts.get_request.side_effect = lambda operation_id: vc.ApprovedOperation(
        original, approval) if operation_id == original.identity.operation_id else vc.ApprovedOperation(compensation, None)
    rig.facts.lookup_request.side_effect = lambda binding, key: vc.RequestHistory(
        (replace(original_fact, status=vc.FactStatus.APPLIED_UNVERIFIED),) if case == "ambiguous" else (original_fact,),
        False) if key == original.identity.idempotency_key else vc.RequestHistory((), False)
    if case == "external_drift":
        rig.state["description"] = "external edit after restore"
    if case == "stale_post_stage":
        with pytest.raises(PermissionError, match="exact original post-stage"):
            service.compensate(original.identity.operation_id, compensation.identity.operation_id, rig.executor)
    elif case in {"no_policy", "ambiguous"}:
        with pytest.raises(PermissionError):
            service.compensate(original.identity.operation_id, compensation.identity.operation_id, rig.executor)
    else:
        result = service.compensate(original.identity.operation_id, compensation.identity.operation_id, rig.executor)
        assert result is not None, "Compensation must be a fresh guarded operation"
        assert result.status == (vc.OperationStatus.CONFLICTED if case == "external_drift" else vc.OperationStatus.CONFIRMED)
    assert rig.transport.patch_config_once.call_count == (2 if case == "authorized" else 1)
