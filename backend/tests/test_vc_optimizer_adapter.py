"""M10 VC/1.0 seam tests: backend services are Protocol fakes only."""

from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.optimizer_adapter import (
    ChampionArtifact, OptimizerChampionAdapter, source_digest,
)
from backend.tests.vc_fakes.fixtures import binding_fixture, executor_fixture


@pytest.fixture
def rig():
    binding = binding_fixture()
    executor = executor_fixture()
    payload = {"version": 2, "instructions": {}}
    source = ChampionArtifact("run", "champion", binding, "a" * 64,
                              str(uuid4()), payload, "description",
                              source_digest(payload, "description"), "requester", None)
    sources = Mock()
    sources.load.return_value = source
    gate = Mock(spec=vc.MutationGate)
    gate.execute.return_value = vc.OperationResult(str(uuid4()), vc.OperationStatus.CONFIRMED,
                                                   None, None, False, ())
    requests = Mock()
    requests.prepare.side_effect = lambda request, requester: request
    flags = Mock()
    flags.enabled.return_value = True
    adapter = OptimizerChampionAdapter(sources=sources, gate=gate, requests=requests, flags=flags)
    return adapter, source, executor, gate, requests, flags


def test_final_champion_applies_once_through_shared_gate(rig):
    adapter, source, executor, gate, requests, _ = rig
    receipt = adapter.apply(source.run_id, source.champion_id, source.binding,
                            source.expected_base, executor)
    assert receipt is gate.execute.return_value
    request, job = gate.execute.call_args.args
    assert job is executor
    assert request.operation_type == "optimizer_apply"
    assert request.origin == vc.Origin.OPTIMIZER
    assert (request.optimizer_run_id, request.champion_id) == ("run", "champion")
    assert request.serialized_space == source.serialized_space
    requests.prepare.assert_called_once_with(request, "requester")
    gate.execute.assert_called_once()


def test_champion_apply_rejects_app_executor_or_tampered_source(rig):
    adapter, source, executor, gate, _, _ = rig
    with pytest.raises(PermissionError):
        adapter.apply("run", "champion", source.binding, source.expected_base,
                      replace(executor, execution_ref="app/worker"))
    adapter.sources.load.return_value = replace(source, payload_digest="b" * 64)
    with pytest.raises(ValueError, match="digest"):
        adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    gate.execute.assert_not_called()


def test_optimizer_write_switches_default_off(rig):
    adapter, source, executor, gate, _, flags = rig
    adapter.flags = None
    with pytest.raises(PermissionError):
        adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    gate.execute.assert_not_called()
