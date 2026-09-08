import ast
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, Mock

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


@pytest.mark.parametrize("entry", ["jobs", "benchmark_push", "enrichment", "loop_enrichment"])
def test_no_job_task_reaches_a_managed_patch_without_a_candidate_session(entry, monkeypatch):
    from genie_space_optimizer.integration import version_control
    from genie_space_optimizer.optimization import preflight, space_quality_enrichment, unified_loop

    client = MagicMock()
    spark = MagicMock()
    if entry == "jobs":
        jobs = Path(version_control.__file__).parents[1] / "jobs"
        guarded = []
        for name in ("run_intake_and_snapshot", "run_benchmark_qc_and_repair",
                     "run_optimize", "run_publish_and_audit"):
            tree = ast.parse((jobs / f"{name}.py").read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in {"patch_space_config", "update_space_description"}
                    if node.func.id not in {"preflight_push_benchmarks_to_space", "run_unified_optimization_loop"}:
                        continue
                    enclosing = [block for block in ast.walk(tree) if isinstance(block, ast.Try)
                                 and any(node in ast.walk(statement) for statement in block.body)]
                    assert any(isinstance(block.body[0], ast.Expr)
                               and isinstance(block.body[0].value, ast.Call)
                               and isinstance(block.body[0].value.func, ast.Name)
                               and block.body[0].value.func.id == "require_candidate_session_for_job"
                               for block in enclosing), f"{name}: {node.func.id} lacks explicit isolation refusal"
                    guarded.append(node.func.id)
        assert sorted(guarded) == ["preflight_push_benchmarks_to_space", "run_unified_optimization_loop"]
        with pytest.raises(PermissionError, match="M10 deployment blocker.*CandidateSession"):
            version_control.require_candidate_session_for_job()
    elif entry == "benchmark_push":
        with pytest.raises(PermissionError):
            preflight.preflight_push_benchmarks_to_space(
                client, spark, "run", "live", "cat", "sch",
                [{"id": "q1", "question": "Count?", "expected_sql": "SELECT 1", "validation_status": "valid"}],
            )
    elif entry == "enrichment":
        with pytest.raises(PermissionError):
            space_quality_enrichment.run_space_quality_enrichment(
                client, spark, run_id="run", space_id="live", raw_config={}, catalog="cat", schema="sch",
            )
    else:
        session = version_control.CandidateSession(
            EvaluationBinding("workspace", "candidate", "run"), IsolationRegistry(), client, writes_enabled=True,
        )
        monkeypatch.setattr(unified_loop, "fetch_space_config", Mock(return_value={}))
        monkeypatch.setattr(unified_loop, "run_space_quality_enrichment",
                            Mock(side_effect=PermissionError("enrichment isolation refused")))
        evaluate = Mock()
        monkeypatch.setattr(unified_loop, "_native_eval", evaluate)
        with pytest.raises(PermissionError, match="enrichment isolation refused"):
            unified_loop.run_unified_optimization_loop(
                client, spark, run_id="run", space_id="candidate", benchmarks=[], catalog="cat", schema="sch",
                levers=[], max_attempts=1, target_accuracy=90, candidate_session=session,
            )
        evaluate.assert_not_called()
    assert client.mock_calls == []
    assert spark.mock_calls == []


@pytest.mark.parametrize("client_kind", ["raw", "none"])
@pytest.mark.parametrize("entry", ["config", "description", "patch_set", "patch_set_both", "rollback",
                                   "auto_apply_prompt_matching", "auto_apply_prompt_matching_legacy", "legacy_em"])
def test_all_legacy_mutation_entrypoints_deny_unproven_clients(entry, client_kind, monkeypatch):
    from genie_space_optimizer.common.genie_client import patch_space_config, update_space_description
    from genie_space_optimizer.optimization import applier

    client = Mock() if client_kind == "raw" else None
    message = "shared UC" if entry == "patch_set_both" else None
    with pytest.raises(PermissionError, match=message):
        if entry == "config":
            patch_space_config(client, "live", {})
        elif entry == "description":
            update_space_description(client, "live", "changed")
        elif entry in {"patch_set", "patch_set_both"}:
            applier.apply_patch_set(client, "live", [], {},
                                    apply_mode="both" if entry == "patch_set_both" else "genie_config")
        elif entry == "rollback":
            applier.rollback({"pre_snapshot": {}}, client, "live")
        elif entry == "legacy_em":
            applier._legacy_apply_em(client, "live", {})
        else:
            monkeypatch.setattr(applier, "ENABLE_SMARTER_SCORING", entry == "auto_apply_prompt_matching")
            applier.auto_apply_prompt_matching(client, "live", {})
    if client is not None:
        assert client.mock_calls == []
