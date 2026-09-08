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


def test_no_job_task_reaches_a_managed_patch_without_a_candidate_session():
    from genie_space_optimizer.optimization.preflight import preflight_push_benchmarks_to_space
    from genie_space_optimizer.optimization.space_quality_enrichment import (
        run_space_quality_enrichment,
    )

    raw_client = MagicMock()
    with pytest.raises(PermissionError):
        preflight_push_benchmarks_to_space(
            raw_client, Mock(), "run", "managed", "catalog", "schema",
            [{
                "id": "q1", "question": "question", "expected_sql": "SELECT 1",
                "validation_status": "valid",
            }],
        )
    assert raw_client.mock_calls == []

    with pytest.raises(PermissionError):
        run_space_quality_enrichment(
            MagicMock(), Mock(), run_id="run", space_id="managed", raw_config={
                "description": "Sales analytics for order reporting.",
                "_parsed_space": {
                    "version": 2,
                    "data_sources": {
                        "tables": [{
                            "identifier": "main.sales.orders",
                            "description": ["Orders"],
                            "column_configs": [],
                        }],
                        "metric_views": [],
                        "functions": [],
                    },
                    "instructions": {"text_instructions": []},
                    "config": {},
                },
            },
            catalog="catalog", schema="schema",
        )

    package_root = Path(__file__).resolve().parents[1]
    jobs = package_root / "src" / "genie_space_optimizer" / "jobs"
    optimize_source = (jobs / "run_optimize.py").read_text()
    assert "candidate_session=candidate_session" in optimize_source
    assert "isolated candidate-Space lifecycle not yet implemented" in optimize_source

    preflight_source = (
        package_root / "src" / "genie_space_optimizer" / "optimization" / "preflight.py"
    ).read_text()
    client_source = (
        package_root / "src" / "genie_space_optimizer" / "common" / "genie_client.py"
    ).read_text()
    enrichment_source = (
        package_root / "src" / "genie_space_optimizer" / "optimization"
        / "space_quality_enrichment.py"
    ).read_text()
    loop_source = (
        package_root / "src" / "genie_space_optimizer" / "optimization" / "unified_loop.py"
    ).read_text()
    preflight_start = preflight_source.index("def preflight_push_benchmarks_to_space")
    publisher_start = client_source.index("def publish_benchmarks_to_genie_space_with_report")
    enrichment_start = enrichment_source.index("def run_space_quality_enrichment")
    loop_enrichment_start = loop_source.index("enrichment_result = run_space_quality_enrichment")
    assert client_source.index("assert_candidate_write", publisher_start) < client_source.index(
        "config = fetch_space_config", publisher_start)
    assert "except PermissionError:\n            raise" in preflight_source[preflight_start:]
    assert "except PermissionError:\n        raise" in enrichment_source[enrichment_start:]
    assert "except PermissionError:\n        raise\n    except Exception:" in loop_source[
        loop_enrichment_start:
    ]


@pytest.mark.parametrize(
    "entry",
    ["config", "description", "patch_set", "rollback", "prompt_matching", "shared_uc"],
)
@pytest.mark.parametrize("client_kind", ["mock", "none"])
def test_all_legacy_mutation_entrypoints_deny_unproven_clients(entry, client_kind):
    from genie_space_optimizer.common.genie_client import patch_space_config, update_space_description
    from genie_space_optimizer.optimization.applier import (
        apply_patch_set,
        auto_apply_prompt_matching,
        rollback,
    )

    client = Mock() if client_kind == "mock" else None
    expected = "shared UC" if entry == "shared_uc" else "candidate proof"
    with pytest.raises(PermissionError, match=expected):
        if entry == "config":
            patch_space_config(client, "live", {})
        elif entry == "description":
            update_space_description(client, "live", "changed")
        elif entry == "patch_set":
            apply_patch_set(client, "live", [], {}, apply_mode="genie_config")
        elif entry == "rollback":
            rollback({"pre_snapshot": {}}, client, "live")
        elif entry == "prompt_matching":
            auto_apply_prompt_matching(client, "live", {})
        else:
            apply_patch_set(client, "live", [], {}, apply_mode="both")
    if client is not None:
        assert client.mock_calls == []
