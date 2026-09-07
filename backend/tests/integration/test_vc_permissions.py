"""Real-platform permission / grant / topology probes (plan tasks 5, 6, 14 + topology).

These are DEPLOYMENT BLOCKERS, not passing tests: the `live_platform` fixture
(backend/tests/integration/conftest.py) FAILS with an explicit blocker reason when
VC_INTEGRATION_CONFIG is unset, so an unconfigured environment can never be scored as
green. They are deselected by default (addopts = -m 'not integration') and only run
under the sanctioned gate:

    python -m pytest backend/tests/integration -m integration -q
"""

import pytest

from backend.services.version_control import platform


@pytest.mark.integration
def test_runtime_principal_cannot_update_delete_or_alter_fact_tables(live_platform):
    for name in ("genie_space_versions", "genie_space_registry", "genie_space_operations"):
        table = live_platform.table(name)
        assert table["properties"]["delta.appendOnly"] == "true"
        assert table["owner"] != live_platform.principal("runtime")
        live_platform.assert_sql_succeeds("runtime", f"{name}.insert")
        for action in ("update", "delete", "alter", "drop", "replace"):
            live_platform.assert_sql_denied("runtime", f"{name}.{action}")


@pytest.mark.integration
def test_executor_cannot_insert_coordination_and_enrollment_is_serialized(live_platform):
    live_platform.assert_sql_denied("executor", "genie_ops_coordination.insert")
    live_platform.assert_sql_succeeds("executor", "genie_ops_coordination.update")
    live_platform.assert_sql_succeeds("enrollment", "genie_ops_coordination.enroll")
    table = live_platform.table("genie_ops_coordination")
    assert table["properties"]["delta.isolationLevel"] == "Serializable"
    assert len({table["owner"], live_platform.principal("executor"),
                live_platform.principal("enrollment")}) == 3
    job = live_platform.api("provisioner", "get",
                            f"/api/2.2/jobs/get?job_id={live_platform.config['enrollment_job_id']}")
    assert job["settings"]["max_concurrent_runs"] == 1
    assert job["settings"]["queue"]["enabled"] is True
    assert job["settings"]["run_as"]["service_principal_name"] == live_platform.principal("enrollment")


@pytest.mark.integration
def test_package_approval_receipt_securables_have_separate_writers(live_platform):
    expected = {"vc_snapshots": "observer", "vc_approval_evidence": "approval",
                "vc_outbound_packages": "source", "vc_target_receipts": "executor"}
    for volume, writer in expected.items():
        live_platform.assert_sql_succeeds(writer, f"{volume}.write")
        for other in set(expected.values()) - {writer}:
            live_platform.assert_sql_denied(other, f"{volume}.write")
    live_platform.assert_sql_succeeds("executor", "vc_outbound_packages.read")
    live_platform.assert_sql_succeeds("source", "vc_target_receipts.read")
    for action in ("insert", "update", "delete", "alter"):
        live_platform.assert_sql_denied("source", f"genie_space_operations.{action}")
    live_platform.assert_sql_denied("source", "genie_space_operations.read")


@pytest.mark.integration
def test_source_target_artifact_topology_is_verified_without_remote_write_credentials(live_platform):
    source = live_platform.api("source", "get", "/api/2.1/unity-catalog/metastore_summary")
    target = live_platform.api("executor", "get", "/api/2.1/unity-catalog/metastore_summary")
    live_platform.assert_sql_succeeds("executor", "vc_outbound_packages.read")
    live_platform.assert_sql_succeeds("source", "vc_target_receipts.read")
    live_platform.assert_sql_denied("source", "vc_target_receipts.write")
    live_platform.assert_sql_denied("source", "genie_space_operations.insert")
    assert source["metastore_id"] == live_platform.config["topology"]["source_metastore"]
    assert target["metastore_id"] == live_platform.config["topology"]["target_metastore"]
    assert platform.topology_read_ready(live_platform.config["topology"])
