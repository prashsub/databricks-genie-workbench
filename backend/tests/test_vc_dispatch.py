"""Target operation polling and native Job retry tests."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts as vc
from backend.tests.test_vc_packages import package_rig, uid
from backend.tests.test_vc_promotion import promotion_rig, approve


def test_polling_discovers_only_local_approved_pending_operations(promotion_rig):
    rig = promotion_rig
    approve(rig)
    local = vc.ApprovedOperation(rig.request, rig.approval)
    wrong = vc.ApprovedOperation(replace(rig.request, binding=rig.source), rig.approval)
    unapproved = vc.ApprovedOperation(replace(rig.request, identity=replace(rig.request.identity,
        operation_id=uid(8))), None)
    completed = replace(rig.request, identity=replace(rig.request.identity, operation_id=uid(9)))
    operations = {uid(4): local, uid(7): wrong, uid(8): unapproved,
                  uid(9): vc.ApprovedOperation(completed, rig.approval)}
    rig.facts.get_request.side_effect = operations.__getitem__
    rig.service.pending = Mock()
    rig.service.pending.scan.return_value = SimpleNamespace(items=(
        vc.OperationHandle(uid(4), vc.OperationStatus.REQUESTED, None),
        vc.OperationHandle(uid(7), vc.OperationStatus.REQUESTED, None),
        vc.OperationHandle(uid(8), vc.OperationStatus.REQUESTED, None),
        vc.OperationHandle(uid(9), vc.OperationStatus.CONFIRMED, 'job/old')), next_cursor='next')
    rig.service.dispatcher = Mock(spec=vc.JobDispatcher)
    rig.service.dispatcher.submit_local.return_value = vc.OperationHandle(uid(4), vc.OperationStatus.REQUESTED, 'job/new')
    page = rig.service.dispatch_pending('target', 'cursor')
    assert [item.operation_id for item in page.items] == [uid(4)]
    assert page.next_cursor == 'next'
    rig.service.pending.scan.assert_called_once_with('target', 'cursor')
    rig.service.dispatcher.submit_local.assert_called_once_with(uid(4), 'promotion')
    rig.gate.execute.assert_not_called()
    with pytest.raises(PermissionError):
        rig.service.dispatch_pending('source', None)


@pytest.mark.parametrize('retry', ['completed', 'unresolved', 'changed_digest', 'ambiguous'])
def test_duplicate_dispatch_or_job_retry_does_not_replay_mutation(promotion_rig, retry):
    rig = promotion_rig
    approve(rig)
    rig.service.execute(uid(4), rig.executor)
    fact = Mock(spec=vc.OperationFact, request=rig.request.identity, fact_kind=vc.FactKind.OPERATION,
                status=vc.FactStatus.CONFIRMED, operation_id=uid(4))
    if retry == 'unresolved':
        fact.status = vc.FactStatus.APPLIED_UNVERIFIED
    if retry == 'changed_digest':
        fact.request = replace(rig.request.identity, request_digest='e' * 64)
    rig.facts.lookup_request.return_value = vc.RequestHistory((fact,), retry == 'ambiguous')
    if retry in {'changed_digest', 'ambiguous'}:
        with pytest.raises(ValueError):
            rig.service.execute(uid(4), rig.executor)
    else:
        rig.service.execute(uid(4), rig.executor)
        rig.gate.verify_only.assert_called_once_with(uid(4), rig.executor)
    rig.gate.execute.assert_called_once()


def test_native_promotion_job_accepts_operation_id_only(promotion_rig):
    from backend.jobs import vc_promote
    rig = promotion_rig
    service = Mock()
    vc_promote.run(uid(4), service=service, executor=rig.executor)
    service.execute.assert_called_once_with(uid(4), rig.executor)
    with pytest.raises(ValueError):
        vc_promote.run('not-an-operation-id', service=service, executor=rig.executor)
