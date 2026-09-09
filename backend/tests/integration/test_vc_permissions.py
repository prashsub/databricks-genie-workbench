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
def test_runtime_fact_writes_are_append_guarded_and_tamper_evident(live_platform):
    # PLATFORM REALITY (proved live): UC has no INSERT-only privilege and MODIFY
    # also confers ALTER/CREATE OR REPLACE, so a fact writer can technically flip
    # delta.appendOnly and rewrite history — no grant configuration makes a UC
    # table immutable to its own writer. The reframed guarantee is therefore:
    #   (1) delta.appendOnly rejects accidental UPDATE/DELETE while set,
    #   (2) destructive DROP still requires ownership (MANAGE), and
    #   (3) every commit is durably attributed in Delta history, so tampering is
    #       detectable (the flip itself is covered by the tamper-evidence test).
    for name in ("genie_space_versions", "genie_space_registry", "genie_space_operations"):
        table = live_platform.table(name)
        assert table["properties"]["delta.appendOnly"] == "true"
        assert table["owner"] != live_platform.principal("runtime")
        live_platform.assert_sql_succeeds("runtime", f"{name}.insert")
        for action in ("update", "delete"):
            live_platform.assert_sql_appendonly_rejected("runtime", f"{name}.{action}")
        live_platform.assert_sql_denied("runtime", f"{name}.drop")
        latest = live_platform.describe_history(name, limit=1)[0]
        assert latest["operation"] in {"WRITE", "MERGE", "COPY INTO"}
        assert latest["userName"] == live_platform.principal("runtime")


@pytest.mark.integration
def test_fact_table_tampering_is_detectable_in_delta_history(live_platform):
    # A SELECT+MODIFY holder CAN flip delta.appendOnly (UC platform reality). The
    # guarantee is that the mutation is durably recorded and attributed, not that
    # it is prevented — run on a disposable owner-created table so the real fact
    # tables are never mutated.
    name = live_platform.provision_scratch_appendonly_table("runtime")
    fqn = f"{live_platform.config['namespace']}.{name}"
    try:
        flip = live_platform.exec_sql(
            "runtime", f"ALTER TABLE {fqn} SET TBLPROPERTIES ('delta.appendOnly' = 'false')")
        assert flip["status"]["state"] == "SUCCEEDED"  # documents the MODIFY reality
        tamper = [row for row in live_platform.describe_history(name, limit=5)
                  if row["operation"] == "SET TBLPROPERTIES"]
        assert tamper, "appendOnly flip must be recorded in Delta history"
        assert tamper[0]["userName"] == live_platform.principal("runtime")
        drop = live_platform.exec_sql("runtime", f"DROP TABLE {fqn}")
        assert drop["status"]["state"] == "FAILED"  # teardown remains owner-only
    finally:
        live_platform.drop_owner_table(name)


@pytest.mark.integration
def test_coordination_writers_are_distinct_serialized_sps(live_platform):
    # UC cannot split UPDATE-only vs INSERT-only, so executor and enrollment are
    # distinct SPs both holding SELECT+MODIFY; the write separation is enforced by
    # the coordination protocol: Serializable isolation + a serialized (single
    # concurrent run, queued) enrollment Job run as the enrollment SP + CAS.
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
    # WRITE VOLUME is a file-level privilege (no SQL DML), probed via the Files API.
    expected = {"vc_snapshots": "observer", "vc_approval_evidence": "approval",
                "vc_outbound_packages": "source", "vc_target_receipts": "executor"}
    for volume, writer in expected.items():
        live_platform.assert_volume_write_succeeds(writer, volume)
        for other in set(expected.values()) - {writer}:
            live_platform.assert_volume_write_denied(other, volume)
    live_platform.assert_volume_read_succeeds("executor", "vc_outbound_packages")
    live_platform.assert_volume_read_succeeds("source", "vc_target_receipts")
    # Source has no grant on the operations fact table: every DML and read denied.
    for action in ("insert", "update", "delete", "alter"):
        live_platform.assert_sql_denied("source", f"genie_space_operations.{action}")
    live_platform.assert_sql_denied("source", "genie_space_operations.read")


@pytest.mark.integration
def test_source_target_artifact_topology_is_verified_without_remote_write_credentials(live_platform):
    source = live_platform.api("source", "get", "/api/2.1/unity-catalog/metastore_summary")
    target = live_platform.api("executor", "get", "/api/2.1/unity-catalog/metastore_summary")
    live_platform.assert_volume_read_succeeds("executor", "vc_outbound_packages")
    live_platform.assert_volume_read_succeeds("source", "vc_target_receipts")
    live_platform.assert_volume_write_denied("source", "vc_target_receipts")
    live_platform.assert_sql_denied("source", "genie_space_operations.insert")
    assert source["metastore_id"] == live_platform.config["topology"]["source_metastore"]
    assert target["metastore_id"] == live_platform.config["topology"]["target_metastore"]
    assert platform.topology_read_ready(live_platform.config["topology"])
