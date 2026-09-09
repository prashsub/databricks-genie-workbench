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

# In-Job OAuth M2M secret env-var names per role (governed credential storage).
# The vc-promotion Job task populates these from a target-workspace secret scope
# (``{{secrets/<scope>/<key>}}``); ``PlatformAdapters.client`` reads them to build
# each role's client. The operator/local path uses named profiles instead and
# never sets these.
_ROLE_SECRET_ENV = {
    "executor": ("VC_EXECUTOR_CLIENT_ID", "VC_EXECUTOR_CLIENT_SECRET"),
    "source": ("VC_SOURCE_CLIENT_ID", "VC_SOURCE_CLIENT_SECRET"),
    "approval": ("VC_APPROVAL_CLIENT_ID", "VC_APPROVAL_CLIENT_SECRET"),
    "directory": ("VC_DIRECTORY_CLIENT_ID", "VC_DIRECTORY_CLIENT_SECRET"),
}


def _m2m_auth(role: str, host: str) -> dict:
    client_id_env, client_secret_env = _ROLE_SECRET_ENV[role]
    return {"mode": "m2m", "host": host,
            "client_id_env": client_id_env, "client_secret_env": client_secret_env}


def _secret_bootstrap(scope: str) -> dict:
    """Map each role's M2M secret env var to its key in the target-workspace secret
    scope. The Job's ambient run_as (executor, granted READ on the scope) reads
    these via the Secrets API and populates ``os.environ`` before building the
    runtime, so ``PlatformAdapters.client`` finds the M2M credentials.

    Serverless task ``environment_variables`` (with ``{{secrets/...}}`` refs) is a
    Beta that is not honored on the target workspace, so the Job self-bootstraps
    from the scope instead -- the REST Secrets API returns values to a READ
    principal.
    """
    env: dict[str, str] = {}
    for role, (client_id_env, client_secret_env) in _ROLE_SECRET_ENV.items():
        env[client_id_env] = f"{role}_client_id"
        env[client_secret_env] = f"{role}_client_secret"
    return {"scope": scope, "env": env}


def to_job_config(governed: dict, *, directory: dict, secret_scope: str) -> dict:
    """Augment an ``author()`` config with in-Job OAuth M2M auth (governed
    credential storage) so ``PlatformAdapters.client`` never depends on a local
    profile inside the Job.

    Every role client is built from injected M2M secrets, including the executor:
    the identity provider routes ``profile is None`` selections to the OBO/PAT
    (human) branch, so the target-local *service* executor must present a verified
    OAuth M2M identity, not ambient auth. The ``profile`` field is kept verbatim as
    the identity-provider routing key (``_profiles``); the auth-first ``client()``
    uses the ``auth`` block and ignores it for construction.

    A dedicated least-privileged ``directory`` role (SCIM group/actor resolution
    only) is added and pinned via ``directory_role`` so approver-group checks in
    ``ApprovalService.authorize`` never run as the executor. A ``secret_bootstrap``
    block tells the Job which scope keys back each M2M secret env var.
    """
    job = json.loads(json.dumps(governed))  # deep copy; never mutate the input
    for role in ("executor", "source", "approval"):
        spec = job["roles"][role]
        spec["auth"] = _m2m_auth(role, spec["host"])
    job["roles"]["directory"] = {
        "host": directory["host"],
        "workspace_id": directory["workspace_id"],
        "principal_id": directory["principal_id"],
        # A warehouse is required by PlatformAdapters.sql for any role it might run
        # SQL as; the directory role only resolves SCIM, but pin the target warehouse
        # for uniformity/fail-closed clarity.
        "warehouse_id": job["warehouse_id"],
        "auth": _m2m_auth("directory", directory["host"]),
    }
    job["directory_role"] = "directory"
    job["secret_bootstrap"] = _secret_bootstrap(secret_scope)
    return job


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
        # The target-owned ledger only accepts versions in its trusted target
        # workspace (`DeltaVersionLedger.append_observation` rejects any other
        # `binding.workspace_id`). The source version is therefore *imported* into
        # the target ledger under this target-workspace binding; the real source
        # workspace is carried separately by `source_workspace_id` (topology only).
        # `space_id` keeps the source space for provenance. D2.5a seeds this exact
        # binding, and the Job's `_inputs` requires the ledger row to equal it.
        "workspace_id": target_binding["workspace_id"],
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
