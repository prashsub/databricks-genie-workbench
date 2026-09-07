"""Execute reviewed owner migrations, never author DDL or Genie content."""

import re
from pathlib import Path
from uuid import UUID

from .bundles import preflight_deployment


OWNER_SPECS = (
    ("M02", "table", "genie_space_versions"),
    ("M02", "table", "genie_space_registry"),
    ("M03", "table", "genie_ops_coordination"),
    ("M06", "table", "genie_space_operations"),
    ("M02", "volume", "vc_snapshots"),
    ("M06", "volume", "vc_approval_evidence"),
    ("M07", "volume", "vc_outbound_packages"),
    ("M07", "volume", "vc_target_receipts"),
)


def provision(manifests, runner) -> None:
    specs = {}
    for manifest in manifests:
        key = tuple(manifest.get(field) for field in ("owner", "kind", "name"))
        if (key not in OWNER_SPECS or key in specs
                or manifest.get("idempotent") is not True
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("revision", ""))):
            raise ValueError("Invalid, duplicate, or unreviewed owner spec")
        specs[key] = dict(manifest)
    if set(specs) != set(OWNER_SPECS):
        raise ValueError("All four owner tables and separate Volumes are required")
    for key in OWNER_SPECS:
        if runner.validate_owner_spec(specs[key]) is not True:
            raise ValueError("Unverified owner migration bytes or grants")
    for key in OWNER_SPECS:
        runner.apply_owner_spec(specs[key])
    runner.apply_grants()


def repeatable_sandbox_provision(root, selection, runner):
    if (not selection.get("profile") or selection["profile"].upper() == "DEFAULT"
            or selection.get("target") != "vc-sandbox"
            or not selection.get("package_target")
            or selection.get("disposable_nonproduction") is not True):
        raise ValueError("Explicit nonproduction profile and sandbox target required")
    operation_id = selection["operation_id"]
    if str(UUID(operation_id)) != operation_id:
        raise ValueError("Canonical provisioning operation_id required")
    root = Path(root).resolve()
    preflight_deployment(root)
    if runner.verify_selection(selection) is not True:
        raise PermissionError("Live provisioning identity/host/workspace not verified")
    profile_args = ["--profile", selection["profile"]]
    variables = [f"--var={key}={value}" for key, value in sorted(selection.get("variables", {}).items())]
    root_args = ["--target", selection["target"], *profile_args, *variables]
    before = runner.content_fingerprint()
    previous = None
    for cycle in range(2):
        runner.command(["databricks", "bundle", "validate", "--strict", *root_args], cwd=root)
        runner.command(["databricks", "bundle", "validate", "--strict", "--target", selection["package_target"],
                        *profile_args, *selection.get("package_variables", [])],
                       cwd=root / "packages/genie-space-optimizer")
        runner.command(["databricks", "bundle", "deploy", *root_args], cwd=root)
        runner.command(["databricks", "bundle", "run", "vc-provision", *root_args,
                        "--params", f"operation_id={operation_id}"], cwd=root)
        if runner.content_fingerprint() != before:
            raise PermissionError("Provisioning changed governed Genie content")
        current = runner.provisioning_fingerprint()
        if cycle and current != previous:
            raise PermissionError("Provisioning was not repeatable")
        previous = current
