"""M04 create intent and physical identity durability."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts as vc
from backend.tests.test_vc_mutation_gate import rig, uid


@pytest.fixture
def create_rig(rig):
    provisional = replace(rig.binding, space_id=None)
    request = vc.CreateRequest(rig.identity, provisional,
        vc.CreatePayload("Sales", {"instructions": "new"}, "new"), None)
    registry = Mock(spec=vc.Registry)
    registry.resolve.return_value = provisional
    rig.gate.registry = registry
    rig.facts.get_request.return_value = vc.ApprovedOperation(request, None)
    records = []
    append = rig.facts.append.side_effect
    def persist(fact):
        records.append(fact)
        return append(fact)
    rig.facts.append.side_effect = persist
    rig.facts.lookup_request.side_effect = lambda *args: vc.RequestHistory(tuple(records), False)
    return rig, request, registry


def test_create_lost_response_keeps_provisional_binding_without_replay(create_rig):
    rig, request, registry = create_rig
    rig.transport.create_once.side_effect = TimeoutError("possible orphan")
    result = rig.gate.create(request, rig.executor)
    assert result is not None, "Lost create response must have a durable unresolved result"
    assert result.unresolved
    assert isinstance(result.preimage, vc.CreateIntentRef)
    intent = next(call.args[0] for call in rig.facts.append.call_args_list
                  if call.args[0].fact_kind == vc.FactKind.CREATE_INTENT)
    assert intent.binding.space_id is None
    assert rig.trace.index("fact:requested") < rig.trace.index("admit")
    assert rig.coordination.admit.call_args.args[1] == result.preimage
    registry.bind_created.assert_not_called()
    rig.transport.patch_config_once.assert_not_called()
    again = rig.gate.create(request, rig.executor)
    assert again.unresolved
    rig.transport.create_once.assert_called_once()
    rig.coordination.finish.assert_not_called()


@pytest.mark.parametrize("bind_failure", [False, True])
def test_create_persists_physical_binding_before_follow_on_edit(create_rig, bind_failure):
    rig, request, registry = create_rig
    rig.transport.create_once.return_value = vc.CreateResponse("new-space", {"space_id": "new-space"})
    physical = replace(request.binding, space_id="new-space")
    def bind(*args):
        rig.trace.append("bind")
        if bind_failure:
            raise RuntimeError("binding unavailable")
        registry.resolve.return_value = physical
        rig.state.update({"serialized_space": {"instructions": "new"}, "description": "new"})
        return physical
    registry.bind_created.side_effect = bind
    result = rig.gate.create(request, rig.executor)
    assert result is not None, "Create needs durable identity before authoritative GET"
    rig.transport.patch_config_once.assert_not_called()
    rig.transport.patch_description_once.assert_not_called()
    if bind_failure:
        assert result.unresolved
        rig.transport.get.assert_not_called()
        rig.coordination.finish.assert_not_called()
    else:
        assert result.status == vc.OperationStatus.CONFIRMED
        assert rig.trace.index("bind") < rig.trace.index("get")
        version = rig.versions[result.postimage.version_id]
        assert version.context.binding == physical
        assert version.context.parent_version_id is None
        assert version.context.attempt_id == rig.fence.attempt_id


def test_create_missing_physical_id_never_uses_display_name(create_rig):
    rig, request, registry = create_rig
    rig.transport.create_once.return_value = vc.CreateResponse("", {"title": "Sales"})
    result = rig.gate.create(request, rig.executor)
    assert result.unresolved
    registry.bind_created.assert_not_called()


def test_create_uncommitted_intent_never_reaches_post(create_rig):
    rig, request, registry = create_rig
    rig.facts.lookup_request.side_effect = lambda *args: vc.RequestHistory((), False)
    with pytest.raises(RuntimeError, match="intent was not durably committed"):
        rig.gate.create(request, rig.executor)
    rig.transport.create_once.assert_not_called()
    rig.coordination.reserve.assert_not_called()
    registry.bind_created.assert_not_called()


@pytest.mark.parametrize("mismatch", ["bind_result", "read_back"])
def test_create_mismatched_physical_binding_is_unverified_without_followon(create_rig, mismatch):
    rig, request, registry = create_rig
    rig.transport.create_once.return_value = vc.CreateResponse("new-space", {"space_id": "new-space"})
    physical = replace(request.binding, space_id="new-space")
    other = replace(physical, space_id="other-space")
    registry.bind_created.return_value = other if mismatch == "bind_result" else physical
    registry.resolve.side_effect = [request.binding, other]
    rig.state.update({"serialized_space": {"instructions": "new"}, "description": "new"})
    result = rig.gate.create(request, rig.executor)
    assert result.status == vc.OperationStatus.APPLIED_UNVERIFIED
    assert result.unresolved
    rig.transport.create_once.assert_called_once()
    rig.transport.get.assert_not_called()
    rig.transport.patch_config_once.assert_not_called()
    rig.transport.patch_description_once.assert_not_called()
    rig.coordination.finish.assert_not_called()
