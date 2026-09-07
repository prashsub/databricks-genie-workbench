from pathlib import Path

import pytest

from backend.services.version_control import platform


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
