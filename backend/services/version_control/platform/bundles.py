"""Read-only bundle inventory. Provisioning never owns Genie content."""

from pathlib import Path

import yaml


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
