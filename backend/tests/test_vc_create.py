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
