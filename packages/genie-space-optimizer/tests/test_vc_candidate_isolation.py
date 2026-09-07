from dataclasses import dataclass
from unittest.mock import Mock

import pytest

from genie_space_optimizer.integration.version_control import EvaluationBinding


@dataclass
class IsolationRegistry:
    managed: bool = False
    owner: str | None = "run"

    def resolve_physical(self, workspace_id, space_id):
        return object() if self.managed else None

    def optimizer_owner(self, workspace_id, space_id):
        return self.owner

    def workspace_id_for(self, client):
        return "workspace"


@pytest.mark.parametrize("managed,owner", [(True, "run"), (False, None), (False, "other")])
def test_candidate_evaluation_cannot_target_managed_binding(managed, owner):
    resource = EvaluationBinding("workspace", "candidate", "run")
    with pytest.raises(PermissionError):
        resource.validate(IsolationRegistry(managed, owner))


def test_positive_optimizer_ownership_is_required():
    resource = EvaluationBinding("workspace", "candidate", "run")
    assert resource.validate(IsolationRegistry()) == resource


def test_every_candidate_mutation_uses_optimizer_owned_evaluation_resource():
    from genie_space_optimizer.integration.version_control import CandidateSession

    registry = IsolationRegistry()
    client = Mock()
    session = CandidateSession(EvaluationBinding("workspace", "candidate", "run"), registry,
                               client, writes_enabled=True)
    for method in ("PATCH", "POST", "DELETE"):
        session.client.api_client.do(method, "/api/2.0/genie/spaces/candidate")
    assert client.api_client.do.call_count == 3
    with pytest.raises(PermissionError):
        session.client.api_client.do("PATCH", "/api/2.0/genie/spaces/live")
    with pytest.raises(PermissionError):
        session.client.statement_execution.execute_statement(statement="ALTER TABLE live")
    registry.managed = True
    with pytest.raises(PermissionError):
        session.client.api_client.do("PATCH", "/api/2.0/genie/spaces/candidate")
    assert client.api_client.do.call_count == 3


def test_candidate_writes_default_off():
    from genie_space_optimizer.integration.version_control import CandidateSession

    with pytest.raises(PermissionError):
        CandidateSession(EvaluationBinding("workspace", "candidate", "run"), IsolationRegistry(), Mock())


def test_legacy_loop_refuses_unproven_target_before_work():
    from genie_space_optimizer.optimization.unified_loop import run_unified_optimization_loop

    with pytest.raises(PermissionError):
        run_unified_optimization_loop(Mock(), Mock(), run_id="run", space_id="live",
                                      benchmarks=[], catalog="cat", schema="sch", levers=[],
                                      max_attempts=1, target_accuracy=90)


@pytest.mark.parametrize("entry", ["config", "description", "patch_set", "rollback"])
def test_all_legacy_mutation_entrypoints_deny_unproven_clients(entry):
    from genie_space_optimizer.common.genie_client import patch_space_config, update_space_description
    from genie_space_optimizer.optimization.applier import apply_patch_set, rollback

    client = Mock()
    with pytest.raises(PermissionError):
        if entry == "config":
            patch_space_config(client, "live", {})
        elif entry == "description":
            update_space_description(client, "live", "changed")
        elif entry == "patch_set":
            apply_patch_set(client, "live", [], {}, apply_mode="both")
        else:
            rollback({"pre_snapshot": {}}, client, "live")
    assert client.mock_calls == []
