"""Execute reviewed owner migrations, never author DDL or Genie content."""

import re


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
