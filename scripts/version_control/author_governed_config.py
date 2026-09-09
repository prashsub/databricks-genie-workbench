#!/usr/bin/env python3
"""Author the in-Job governed promotion config from the two-workspace config.

The ``vc-promotion`` Job loads a single reviewed JSON document via
``--governed-config`` (D2.4f) and hands it to ``build_vc_runtime('promotion',
config)`` -> ``build_governed_seams`` / ``PlatformAdapters``. That schema differs
from the operator ``VC_TWO_WORKSPACE_CONFIG`` used by the integration harness:
roles are keyed by *runtime function* (``executor``/``source``/``approval``),
each carries its own ``warehouse_id``, and the executor selection pins the
target-local Job (``execution_ref = job/<promotion_job_id>``) that
``PromotionService._executor`` requires.

This module is a **pure transform** (no live calls): it maps the two-workspace
config into that in-Job schema so the document can be reviewed, uploaded to an
immutable UC Volume during provisioning, and pinned as ``vc_governed_config_uri``.
The topology / capability proofs it emits are the provisioned reality asserted
live by the D1 topology gate and the capabilities_ready probe; ``PlatformAdapters``
reads them back verbatim (``topology_proof`` / ``capability_probe``).
"""

from __future__ import annotations

import json
import os
import sys
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control.platform.capabilities import (
    FIRST_WRITE_CAPABILITIES,
)

# Runtime flags the promotion write path checks (all others default off).
_FLAGS = {"vc_writes_enabled": True, "vc_promotion_enabled": True, "vc_history_enabled": True}


def _source_binding(cfg: dict) -> dict:
    """Stable logical identity for the immutable source version in the ledger.

    The package manifest pins only ``source_version_id`` + digests (the artifact
    is binding-independent via ``portable``), so the source binding is chosen here
    deterministically and must match the ledger seed (D2.5a). Reused verbatim if
    already present in the two-workspace config.
    """
    existing = cfg.get("source_binding")
    if existing:
        return existing
    source = cfg["roles"]["source"]
    target_binding = cfg["target_binding"]
    binding_id = str(uuid5(NAMESPACE_URL,
                           f"vc-source-binding/{source['workspace_id']}/{cfg['source_space_id']}"))
    return {
        "binding_id": binding_id,
        "binding_revision": 1,
        "space_key": f"{target_binding['space_key']}-source",
        "workspace_id": source["workspace_id"],
        "space_id": cfg["source_space_id"],
        "environment": "dev",
    }


def _topology(cfg: dict, source_ws: str, target_ws: str) -> dict:
    """Read-only cross-workspace topology proof (``topology_read_ready`` shape).

    Same-metastore (D1) => a single metastore id + ``shared_uc_grants_verified``;
    an explicit ``cfg['topology']`` overrides these defaults for a cross-metastore
    (Delta Sharing) deployment.
    """
    override = cfg.get("topology", {})
    metastore = override.get("source_metastore", "shared")
    proof = {
        "source_workspace": source_ws,
        "target_workspace": target_ws,
        "source_metastore": metastore,
        "target_metastore": override.get("target_metastore", metastore),
        "remote_write_credentials": False,
        "packages_readable": True,
        "receipts_readable": True,
        "source_target_write_denied": True,
        "shared_uc_grants_verified": True,
    }
    proof.update({k: v for k, v in override.items() if k not in proof})
    return proof


def author(cfg: dict, *, promotion_job_id=None) -> dict:
    """Transform the two-workspace config into the in-Job governed config."""
    job_id = promotion_job_id or cfg.get("promotion_job_id")
    if not job_id:
        raise ValueError("promotion_job_id required (deploy the vc-promotion DAB job first)")
    roles_in = cfg["roles"]
    warehouse = cfg["target_warehouse_id"]
    catalog, control_schema = cfg["target_namespace"].split(".")
    target, source, approval = (roles_in["target"], roles_in["source"], roles_in["approval"])
    source_ws, target_ws = str(source["workspace_id"]), str(target["workspace_id"])

    def _selection(spec: dict, execution_ref: str) -> dict:
        return {"workspace_id": spec["workspace_id"], "host": spec["host"],
                "principal_id": spec["principal_id"], "execution_ref": execution_ref,
                "profile": spec["profile"]}

    governed = {
        "workspace_id": target["workspace_id"],
        "source_workspace_id": source["workspace_id"],
        "catalog": catalog,
        "control_schema": control_schema,
        "warehouse_id": warehouse,
        "target_warehouse_id": warehouse,
        "target_host": target["host"],
        # The fact-table append writer is the executor SP (SELECT+MODIFY on the
        # append-only operation facts); required by the governed-runtime gate.
        "principals": {"runtime": target["principal_id"]},
        "roles": {
            "executor": {**target, "warehouse_id": warehouse},
            "source": {**source, "warehouse_id": warehouse},
            "approval": {**approval, "warehouse_id": warehouse},
        },
        # The executor selection pins the target-local Job; source is read-only
        # (package build) and never on the execute path, so it carries no Job ref.
        "target_selection": _selection(target, f"job/{job_id}"),
        "source_selection": _selection(source, "workbench:source"),
        "source_binding": _source_binding(cfg),
        "volumes": dict(cfg["volumes"]),
        "reverse_receipt_volume": cfg["reverse_receipt_volume"],
        "topology": _topology(cfg, source_ws, target_ws),
        "capabilities": {capability: True for capability in FIRST_WRITE_CAPABILITIES},
        "flags": dict(_FLAGS),
        "promotion_job_id": job_id,
    }
    if cfg.get("config_file"):
        governed["config_file"] = cfg["config_file"]
    return governed


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    location = os.environ.get("VC_TWO_WORKSPACE_CONFIG")
    if not location:
        raise SystemExit("VC_TWO_WORKSPACE_CONFIG must point to the two-workspace config")
    from pathlib import Path

    cfg = json.loads(Path(location).read_text())
    job_id = argv[0] if argv else None
    governed = author(cfg, promotion_job_id=job_id)
    json.dump(governed, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
