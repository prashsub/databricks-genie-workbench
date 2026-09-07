"""Real Jobs/Genie/Delta probes. Missing deployment is a failure, never a skip.

Run explicitly with VC_M10_INTEGRATION_CONFIG pointing at the disposable fixture
manifest described in M10_IMPLEMENTATION.md. No platform mutations run offline.
"""

from datetime import timedelta
import json
import os
from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.integration


def _identifier(value):
    assert isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
    return f"`{value}`"


def _literal(value):
    assert isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value)
    return f"'{value}'"


class LiveOptimizer:
    def __init__(self, config):
        from databricks.sdk import WorkspaceClient

        assert config["disposable_nonproduction"] is True
        assert config["profile"].upper() != "DEFAULT"
        self.config = config
        self.client = WorkspaceClient(profile=config["profile"])
        assert self.client.config.host.rstrip("/") == config["host"].rstrip("/")
        identity = self.client.api_client.do("GET", "/api/2.0/preview/scim/v2/Me",
                                             response_headers=["X-Databricks-Org-Id"])
        assert str(identity["X-Databricks-Org-Id"]) == str(config["workspace_id"])
        self.control = ".".join(_identifier(config[key]) for key in ("catalog", "control_schema"))

    def rows(self, table, predicate):
        from databricks.sdk.service.sql import StatementState

        result = self.client.statement_execution.execute_statement(
            warehouse_id=self.config["warehouse_id"],
            statement=f"SELECT * FROM {self.control}.{_identifier(table)} WHERE {predicate}",
            wait_timeout="50s", on_wait_timeout="CANCEL",
        )
        assert result.status.state == StatementState.SUCCEEDED, "Authoritative Delta read failed"
        assert not result.manifest.truncated, "Probe must not silently truncate evidence"
        assert not result.result.next_chunk_index, "Probe requires complete evidence in one chunk"
        columns = [column.name for column in result.manifest.schema.columns]
        return [dict(zip(columns, row)) for row in (result.result.data_array or [])]

    def state(self, space_id):
        _literal(space_id)
        envelope = self.client.api_client.do("GET", f"/api/2.0/genie/spaces/{space_id}",
                                              query={"include_serialized_space": "true"})
        serialized = envelope["serialized_space"]
        return {"serialized_space": json.loads(serialized) if isinstance(serialized, str) else serialized,
                "description": envelope.get("description")}

    def run(self, case, *, retry=False):
        settings = self.client.jobs.get(case["job_id"]).settings
        assert settings.tags.get("vc_test_environment") == "disposable"
        assert settings.tags.get("vc_module") == "m10"
        assert settings.run_as.service_principal_name == self.config["executor_principal"]
        parameters = dict(case["retry_parameters"] if retry else case["parameters"])
        assert parameters["run_id"] == case["run_id"]
        return self.client.jobs.run_now_and_wait(job_id=case["job_id"], job_parameters=parameters,
                                                 timeout=timedelta(minutes=30))

    def versions(self, case):
        return self.rows("genie_space_versions", "optimizer_run_id = " + _literal(case["run_id"]))

    def operations(self, case):
        return self.rows("genie_space_operations", "operation_id = " + _literal(case["operation_id"]))


@pytest.fixture
def live_optimizer():
    location = os.environ.get("VC_M10_INTEGRATION_CONFIG")
    if not location:
        pytest.fail("DEPLOYMENT BLOCKER: VC_M10_INTEGRATION_CONFIG and M04/M06/M08 composition required")
    config = json.loads(Path(location).read_text())
    return LiveOptimizer(config)


def test_live_candidates_and_abandon_do_not_mutate_managed_binding(live_optimizer):
    live = live_optimizer
    case = live.config["cases"]["candidate_abandon"]
    assert case["candidate_space_id"] != case["managed_space_id"]
    assert not live.rows("genie_space_registry", "space_id = " + _literal(case["candidate_space_id"]))
    before = live.state(case["managed_space_id"])
    candidate_before = live.state(case["candidate_space_id"])
    assert not live.versions(case), "Use a fresh, unapplied run fixture"
    live.run(case)
    assert live.state(case["managed_space_id"]) == before
    assert live.state(case["candidate_space_id"]) != candidate_before
    assert not live.versions(case)
    assert not live.operations(case)


def test_live_champion_retry_preserves_one_authoritative_optimizer_version(live_optimizer):
    live = live_optimizer
    case = live.config["cases"]["champion"]
    live.run(case)
    first = live.versions(case)
    assert len(first) == 1 and first[0]["origin"] == "optimizer"
    assert first[0]["champion_id"] == case["champion_id"]
    assert first[0]["operation_id"] == case["operation_id"]
    observed = live.state(case["managed_space_id"])
    assert json.loads(first[0]["serialized_space_json"]) == observed["serialized_space"]
    assert json.loads(first[0]["restorable_metadata_json"]).get("description") == observed["description"]
    live.run(case, retry=True)
    assert live.versions(case) == first
    assert any(row["status"] == "confirmed" for row in live.operations(case))


def test_live_external_preimage_is_preserved_separately(live_optimizer):
    live = live_optimizer
    case = live.config["cases"]["external_preimage"]
    before = live.state(case["managed_space_id"])
    live.run(case)
    evidence = live.rows("genie_space_versions", "operation_id = " + _literal(case["operation_id"]))
    external = [row for row in evidence if row["origin"] == "external"]
    assert external and json.loads(external[0]["serialized_space_json"]) == before["serialized_space"]
    assert len([row for row in evidence if row["origin"] == "optimizer"]) <= 1
    assert all(row["optimizer_run_id"] is None for row in external)


def test_live_noop_champion_has_durable_receipt_without_version(live_optimizer):
    live = live_optimizer
    case = live.config["cases"]["noop"]
    before = live.state(case["managed_space_id"])
    live.run(case)
    assert live.state(case["managed_space_id"]) == before
    assert not live.versions(case)
    assert any(row["status"] == "noop" for row in live.operations(case))


def test_live_ambiguous_apply_cannot_replay_or_compensate(live_optimizer):
    live = live_optimizer
    case = live.config["cases"]["ambiguous"]
    live.run(case)
    facts = live.operations(case)
    assert any(row["status"] in {"applied_unverified", "applied_partial", "quarantined"} for row in facts)
    attempts = {row["attempt_id"] for row in facts if row["attempt_id"]}
    live.run(case, retry=True)
    retried = live.operations(case)
    assert {row["attempt_id"] for row in retried if row["attempt_id"]} == attempts
    assert not any(row["status"] in {"compensation_attempted", "compensated"} for row in retried)
    assert not live.versions(case)


def test_live_historical_restore_uses_job_and_restored_from_link(live_optimizer):
    live = live_optimizer
    case = live.config["cases"]["restore"]
    completed = live.run(case)
    assert completed.run_id is not None
    versions = live.rows("genie_space_versions", "operation_id = " + _literal(case["operation_id"]))
    restored = [row for row in versions if row["restored_from_version_id"] == case["historical_version_id"]]
    assert len(restored) == 1 and restored[0]["origin"] == "restore"
    assert json.loads(restored[0]["serialized_space_json"]) == live.state(case["managed_space_id"])["serialized_space"]
