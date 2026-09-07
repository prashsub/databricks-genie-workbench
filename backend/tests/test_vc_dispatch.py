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
