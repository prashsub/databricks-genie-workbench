"""Read-only bundle inventory. Provisioning never owns Genie content."""

from pathlib import Path
from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class DeploymentEntry:
    path: str
    transition: str
    authoritative_optimizer: bool = False
    governed_content: bool = False


def deployment_inventory(root: Path) -> tuple[DeploymentEntry, ...]:
    entries = (
        DeploymentEntry("databricks.yml", "Retain sole Workbench optimizer Job authority", True),
        DeploymentEntry("packages/genie-space-optimizer/databricks.yml",
                        "Standalone only; never deploy alongside root into the same installation"),
        DeploymentEntry("scripts/deploy.sh", "Retain app deploy until verified DAB app import/cutover"),
        DeploymentEntry("scripts/deploy_lib/install.py", "Notebook installer; same app cutover barrier"),
        DeploymentEntry("app.yaml", "Preserve app runtime configuration; all VC writers disabled"),
    )
    for entry in entries:
        if not (root / entry.path).is_file():
            raise ValueError(f"Missing deployment path: {entry.path}")
        if entry.path.endswith("databricks.yml"):
            guard_bundle(root / entry.path)
    return entries


def guard_bundle(path: Path) -> tuple[Path, ...]:
    visited = set()
    active = set()

    def inspect(value):
        if isinstance(value, dict):
            if "genie_spaces" in value:
                raise ValueError("DAB dual authority: genie_spaces resource")
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    def visit(current):
        current = current.resolve()
        if current in active:
            raise ValueError("Cyclic bundle include")
        if current in visited:
            return
        active.add(current)
        data = yaml.safe_load(current.read_text())
        if not isinstance(data, dict):
            raise ValueError("Bundle must be a mapping")
        inspect(data)
        includes = data.get("include", [])
        if not isinstance(includes, list):
            raise ValueError("Bundle include must be a list")
        for pattern in includes:
            if not isinstance(pattern, str) or Path(pattern).is_absolute():
                raise ValueError("Unresolved bundle include")
            matches = sorted(current.parent.glob(pattern))
            if not matches:
                raise ValueError(f"Unresolved bundle include: {pattern}")
            for included in matches:
                visit(included)
        active.remove(current)
        visited.add(current)

    visit(Path(path))
    return tuple(sorted(visited))
