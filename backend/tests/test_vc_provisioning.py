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
