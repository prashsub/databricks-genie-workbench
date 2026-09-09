"""Author VC_TWO_WORKSPACE_CONFIG for the same-metastore promotion gate (D1).

Derives the two-workspace promotion config from the already-provisioned
VC_INTEGRATION_CONFIG. Same-metastore realization:
  - source_owner / target_owner are the provisioner SP (it owns the volumes and
    the operations table, is not one of the runtime {source,target,approval} SPs,
    and can read volume details + SHOW GRANTS).
  - reverse_receipt_volume is the shared vc_target_receipts volume: the source SP
    holds READ (not WRITE) on it via shared UC grants, which is the same-metastore
    realization of the reverse receipt share.

Step-3 fields (manifest_uri, source_version_id, promotion_job_id) are filled in
during D2 when the package is built/uploaded and the promotion Job is deployed.
Writes to $VC_TWO_WORKSPACE_CONFIG (or ~/.databricks-vc/two_workspace_config.json).
"""

import json
import os
from pathlib import Path
from uuid import uuid4


def main() -> None:
    src = json.loads(Path(os.environ["VC_INTEGRATION_CONFIG"]).read_text())
    roles = src["roles"]
    catalog, schema = src["namespace"].split(".")
    target_space = src["governed_spaces"][0]

    def role(name):
        r = roles[name]
        return {"profile": r["profile"], "host": r["host"],
                "workspace_id": r["workspace_id"], "principal_id": r["principal_id"]}

    def volume(name):
        return f"/Volumes/{catalog}/{schema}/{name}"

    provisioner = role("provisioner")
    config = {
        "disposable_nonproduction": True,
        "config_file": src.get("config_file"),
        "operation_id": str(uuid4()),
        "roles": {
            "source": role("source"),
            "target": role("executor"),
            "approval": role("approval"),
            # Same-metastore: the provisioner owns the volumes + operations table.
            "source_owner": provisioner,
            "target_owner": provisioner,
        },
        "volumes": {
            "outbound": volume("vc_outbound_packages"),
            "approval": volume("vc_approval_evidence"),
            "receipts": volume("vc_target_receipts"),
        },
        # Shared UC grant realization of the reverse share (source has READ only).
        "reverse_receipt_volume": volume("vc_target_receipts"),
        "target_namespace": src["namespace"],
        "target_warehouse_id": roles["executor"]["warehouse_id"],
        "target_binding": {
            "binding_id": str(uuid4()),
            "binding_revision": 1,
            "space_key": "vc-promotion-gate",
            "workspace_id": roles["executor"]["workspace_id"],
            "space_id": target_space,
            "environment": "prod",
        },
        "source_groups": src.get("topology", {}).get("source_groups", []),
        "local_acceptance_passed": True,
        # --- Step-3 fields, filled during D2 ---
        "manifest_uri": None,
        "source_version_id": None,
        "promotion_job_id": None,
    }

    destination = Path(os.environ.get("VC_TWO_WORKSPACE_CONFIG")
                       or (Path.home() / ".databricks-vc" / "two_workspace_config.json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(config, indent=2))
    print(f"wrote {destination}")
    print(f"operation_id={config['operation_id']}")
    print(f"target_binding.space_id={target_space}")


if __name__ == "__main__":
    main()
