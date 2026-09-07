from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.services.version_control import platform
from backend.tests.integration.conftest import live_platform


ROOT = Path(__file__).resolve().parents[2]


def test_deployment_contains_no_governed_genie_content_resource(tmp_path):
    guard = getattr(platform, "guard_bundle", None)
    assert callable(guard), "Provisioning must reject nested governed content before deploy"
    root = tmp_path / "databricks.yml"
    child = tmp_path / "child.yml"
    root.write_text("include: [child.yml]\nresources: {jobs: {}}\n")
    child.write_text("targets: {prod: {resources: {genie_spaces: {managed: {}}}}}\n")
    with pytest.raises(ValueError, match="genie_spaces"):
        guard(root)
    child.write_text("include: [missing.yml]\n")
    with pytest.raises(ValueError, match="include"):
        guard(root)
    child.write_text("resources: {jobs: {worker: {name: worker}}}\n")
    assert set(guard(root)) == {root, child}
    guard(ROOT / "databricks.yml")


def test_existing_optimizer_bundle_and_app_deploy_paths_remain_accounted_for():
    inventory = getattr(platform, "deployment_inventory", None)
    assert callable(inventory), "Existing deployments need an explicit transition inventory"
    entries = inventory(ROOT)
    assert {entry.path for entry in entries} == {
        "databricks.yml", "packages/genie-space-optimizer/databricks.yml",
        "scripts/deploy.sh", "scripts/deploy_lib/install.py", "app.yaml",
    }
    assert all((ROOT / entry.path).is_file() and entry.transition for entry in entries)
    assert sum(entry.authoritative_optimizer for entry in entries) == 1
    assert all(entry.governed_content is False for entry in entries)


def test_ddl_migrations_are_owned_ordered_idempotent_and_not_content_deploys():
    run = getattr(platform, "provision", None)
    assert callable(run), "Provisioning must validate owner manifests before executing"
    runner = Mock()
    manifests = [
        {"owner": owner, "kind": kind, "name": name, "idempotent": True, "revision": "sha256:" + "a" * 64}
        for owner, kind, name in [
            ("M02", "table", "genie_space_versions"),
            ("M02", "table", "genie_space_registry"),
            ("M03", "table", "genie_ops_coordination"),
            ("M06", "table", "genie_space_operations"),
            ("M02", "volume", "vc_snapshots"),
            ("M07", "volume", "vc_outbound_packages"),
            ("M06", "volume", "vc_approval_evidence"),
            ("M07", "volume", "vc_target_receipts"),
        ]
    ]
    runner.validate_owner_spec.return_value = True
    run(list(reversed(manifests)), runner)
    assert [call.args[0]["kind"] for call in runner.apply_owner_spec.call_args_list] == ["table"] * 4 + ["volume"] * 4
    runner.apply_grants.assert_called_once_with()
    runner.reset_mock()
    for invalid in [manifests[:-1], manifests + [manifests[0]],
                    [dict(manifests[0], owner="M08")] + manifests[1:],
                    [dict(manifests[0], idempotent=False)] + manifests[1:],
                    [dict(manifests[0], revision="unverified")] + manifests[1:],
                    [dict(manifests[0], kind="genie_spaces")] + manifests[1:]]:
        with pytest.raises(ValueError):
            run(invalid, runner)
    runner.apply_owner_spec.assert_not_called()
    runner.apply_grants.assert_not_called()
    runner.validate_owner_spec.return_value = False
    with pytest.raises(ValueError, match="owner"):
        run(manifests, runner)
    runner.apply_owner_spec.assert_not_called()


def test_fact_permissions_require_append_only_nonowner_and_no_modify():
    verify = getattr(platform, "verify_fact_permissions", None)
    assert callable(verify), "Effective fact-table grants must fail closed"
    facts = {name: {"append_only": True, "owner": "provisioner",
                    "principal": "runtime", "privileges": {"SELECT", "INSERT"}}
             for name in ("genie_space_versions", "genie_space_registry", "genie_space_operations")}
    assert verify(facts) is True
    for field, value in (("append_only", False), ("owner", "runtime"),
                         ("privileges", {"SELECT", "MODIFY"}),
                         ("privileges", {"SELECT", "INSERT", "MANAGE"}),
                         ("privileges", {"SELECT", "INSERT", "UPDATE"})):
        bad = {name: dict(row, **{field: value}) for name, row in facts.items()}
        assert verify(bad) is False
    assert verify({}) is False


@pytest.mark.integration
def test_runtime_principal_cannot_update_delete_or_alter_fact_tables(live_platform):
    for name in ("genie_space_versions", "genie_space_registry", "genie_space_operations"):
        table = live_platform.table(name)
        assert table["properties"]["delta.appendOnly"] == "true"
        assert table["owner"] != live_platform.principal("runtime")
        live_platform.assert_sql_succeeds("runtime", f"{name}.insert")
        for action in ("update", "delete", "alter", "drop", "replace"):
            live_platform.assert_sql_denied("runtime", f"{name}.{action}")


def test_coordination_grants_require_separate_serialized_enrollment():
    verify = getattr(platform, "verify_coordination_permissions", None)
    assert callable(verify), "Enrollment must be separate from UPDATE-only executors"
    proof = dict(owner="provisioner", executor="executor", enrollment="enrollment",
                 executor_privileges={"SELECT", "UPDATE"},
                 enrollment_privileges={"SELECT", "INSERT"},
                 max_concurrent_runs=1, queue_enabled=True, isolation="Serializable")
    assert verify(proof) is True
    for key, value in (("executor_privileges", {"SELECT", "MODIFY"}),
                       ("executor_privileges", {"SELECT", "UPDATE", "INSERT"}),
                       ("enrollment_privileges", {"SELECT", "INSERT", "UPDATE"}),
                       ("enrollment", "executor"), ("owner", "executor"),
                       ("max_concurrent_runs", 2), ("queue_enabled", False),
                       ("isolation", "WriteSerializable")):
        assert verify(dict(proof, **{key: value})) is False
    assert verify({}) is False


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


def test_missing_fine_grained_dml_or_serializable_disables_writes():
    check = getattr(platform, "storage_write_ready", None)
    assert callable(check), "Unknown or unsupported capabilities must deny every write"
    valid = dict(fine_grained_dml=True, serializable=True, nonowner_runtime=True,
                 append_only=True, enrollment_isolated=True)
    probe = Mock(return_value=valid)
    assert check(probe) is True
    for capability in valid:
        for value in (False, None, "true", 1):
            probe.return_value = dict(valid, **{capability: value})
            assert check(probe) is False
        probe.return_value = {key: value for key, value in valid.items() if key != capability}
        assert check(probe) is False
    probe.side_effect = TimeoutError("warehouse unavailable")
    assert check(probe) is False
    assert check(None) is False


def test_bundle_inventory_guard_rejects_governed_content_and_exports_detection_evidence(tmp_path):
    scan = getattr(platform, "bundle_detection_evidence", None)
    assert callable(scan), "M05 needs read-only dual-authority detection evidence"
    root = tmp_path / "databricks.yml"
    child = tmp_path / "child.yml"
    root.write_text("include: [child.yml]\n")
    child.write_text("resources: {genie_spaces: {managed: {}}}\n")
    evidence = scan(root)
    assert evidence.status == "dual_authority"
    assert evidence.schema_version == "VC/1.0"
    assert str(child) in evidence.file_digests
    assert evidence.findings == ("resources.genie_spaces",)
    assert not hasattr(evidence, "actor_id")
    original = child.read_bytes()
    import subprocess
    import sys
    command = [sys.executable, str(ROOT / "scripts/version_control/bundle_guard.py"), str(root)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "dual_authority" in result.stdout
    assert child.read_bytes() == original
    child.write_text("resources: {jobs: {worker: {name: worker}}}\n")
    assert scan(root).status == "clear"
    assert subprocess.run(command, capture_output=True).returncode == 0
    child.unlink()
    assert scan(root).status == "unknown"
    deploy = (ROOT / "scripts/deploy.sh").read_text()
    assert deploy.index("_preflight_check_vc_bundle_content") < deploy.index("DEPLOYER=$(databricks current-user")
    installer = (ROOT / "scripts/deploy_lib/install.py").read_text()
    assert installer.index("preflight_deployment(Path(cfg.repo_root") < installer.index("ensure_app(w, cfg)")


def test_artifact_grants_require_distinct_volume_securables_and_writers():
    verify = getattr(platform, "verify_artifact_permissions", None)
    assert callable(verify), "Volume prefixes are not separate grant boundaries"
    writers = dict(vc_snapshots="observer", vc_approval_evidence="approval-service",
                   vc_outbound_packages="source", vc_target_receipts="target")
    volumes = {name: {"securable": f"catalog.control.{name}", "writers": {writer},
                      "owner": "provisioner", "privileges_verified": True}
               for name, writer in writers.items()}
    assert verify(volumes, writers, {"target"}, {"source"}) is True
    for name in ("vc_approval_evidence", "vc_target_receipts"):
        bad = {key: dict(row) for key, row in volumes.items()}
        bad[name]["writers"] = {"source", writers[name]}
        assert verify(bad, writers, {"target"}, {"source"}) is False
    shared = {key: dict(row, securable="catalog.control.shared") for key, row in volumes.items()}
    assert verify(shared, writers, {"target"}, {"source"}) is False
    assert verify(volumes, writers, {"target", "source"}, {"source"}) is False
    assert verify(volumes, writers, {"target"}, {"source", "provisioner"}) is False


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
