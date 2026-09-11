"""D2.3: build + upload the two-workspace promotion package (same-metastore).

Captures the source Genie space as the source SP, builds a content-addressed
package (manifest + artifact/mapping/validation) mapping the source table to the
target table with the target environment, verifies offline that the rendered
artifact DIFFERS from the target space's current config (so the promotion is a
real write), uploads the four files to the outbound Volume as the source SP, and
records manifest_uri / source_version_id in VC_TWO_WORKSPACE_CONFIG.
"""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from databricks.sdk import WorkspaceClient

from backend.services.config_fingerprint import Canonicalizer
from backend.services.version_control import contracts as vc
from backend.services.version_control.promotion import packages
from backend.services.version_control.promotion.mapping import MappingTransformer


def _client(profile, host):
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("DATABRICKS_")}
    try:
        return WorkspaceClient(profile=profile, host=host, auth_type="oauth-m2m")
    finally:
        os.environ.update(saved)


def target_folder(config: dict) -> str:
    """The promotion target parent folder (deterministic from the admin user).

    Shared by the package builder and the D2.5b mint driver so both derive the
    identical mapping (hence identical ``mapping_digest``)."""
    admin = os.environ.get("VC_ADMIN_USER", "").strip()
    return f"/Users/{admin}" if admin else "/Workspace"


def source_space_key(config: dict) -> str:
    """The source space_key the immutable version binds to.

    This flows into the package manifest's ``ownership.space_key`` (and hence the
    ``package_digest``). It MUST match the ledger version the mint driver releases
    from -- that version is bound to the *source* space, not the target -- or the
    D2.3 fixture package diverges from the real released package by ownership alone.
    Falls back to the target space_key only when no source binding is configured."""
    return config.get("source_binding", {}).get("space_key") or config["target_binding"]["space_key"]


def _consumers(config: dict) -> tuple[str, ...]:
    """Principals declared as consumers of the promoted target space. Explicit
    ``consumers`` in the config wins; otherwise default to the target executor SP
    (the gate operator). Sorted + deduped for a deterministic mapping digest."""
    declared = config.get("consumers")
    if declared:
        return tuple(sorted(set(declared)))
    principal = config.get("roles", {}).get("target", {}).get("principal_id")
    return (principal,) if principal else ()


def build_mapping_policy(config: dict) -> tuple[vc.MappingSpec, vc.TestPolicy]:
    """Build the exact source->target mapping + test policy the package was built
    from. Kept as the single source of truth so the D2.5b release re-creates the
    same request the pinned manifest describes (verified by ``mapping_digest``)."""
    target_binding = vc.from_wire(vc.BindingRef, config["target_binding"])
    mappings = {
        config["source_table"]: config["target_table"],
        "target:warehouse_id": config["target_warehouse_id"],
        "target:parent_path": target_folder(config),
    }
    # Declare the promoted space's consumers so preflight can prove each can use the
    # target warehouse/folder/tables. For the two-workspace gate the target executor
    # SP is the operator/consumer; it is granted SELECT on the target table, CAN_USE
    # on the warehouse, and folder access during D2.5 provisioning.
    for index, consumer in enumerate(_consumers(config)):
        mappings[f"target:consumer:{index}"] = consumer
    provisional = vc.MappingSpec("VC/1.0", target_binding, mappings, "0" * 64, "vc-map/1")
    mapping = replace(provisional, mapping_digest=packages.mapping_digest(provisional))
    thresholds = {"validation": {"smoke": True}, "benchmark": {"minimum": 1}}
    policy = vc.TestPolicy("VC/1.0", packages.digest(thresholds["validation"]),
                           packages.digest(thresholds["benchmark"]), thresholds)
    return mapping, policy


def main() -> None:
    path = os.environ["VC_TWO_WORKSPACE_CONFIG"]
    config = json.loads(Path(path).read_text())
    roles = config["roles"]
    src_role, tgt_role = roles["source"], roles["target"]
    canonicalizer = Canonicalizer()
    source = _client(src_role["profile"], src_role["host"])
    target = _client(tgt_role["profile"], tgt_role["host"])

    # 1. Capture the immutable source version.
    src_envelope = source.api_client.do(
        "GET", f"/api/2.0/genie/spaces/{config['source_space_id']}",
        query={"include_serialized_space": "true"})
    snapshot = canonicalizer.observe(src_envelope)
    source_binding = vc.BindingRef(str(uuid4()), 1, source_space_key(config),
                                   src_role["workspace_id"], config["source_space_id"], "dev")
    # Reuse the pinned source_version_id if the ledger was already seeded (D2.5a):
    # the manifest embeds only the version id + content digests (not the binding),
    # so re-running D2.3 to refresh the mapping/consumer digests MUST keep the same
    # version id, or the seeded ledger row (idempotent on observation_key) diverges.
    version_id = config.get("source_version_id") or str(uuid4())
    version = vc.Version(version_id, snapshot, vc.CaptureContext(
        source_binding, "capture", datetime.now(UTC), "initial",
        vc.ActorContext(src_role["principal_id"], src_role["workspace_id"], "service"),
        vc.Origin.WORKBENCH))

    # 2. Mapping (source->target identifier + target environment) and policy.
    target_binding = vc.from_wire(vc.BindingRef, config["target_binding"])
    mapping, policy = build_mapping_policy(config)

    # 3. Build the content-addressed package.
    manifest, files = packages.build(version, mapping, policy)

    # 4. Offline proof: rendered artifact differs from the target's current state,
    #    so the promotion Job performs a real write (never a no-op).
    artifact = json.loads(files["artifact.json"])
    rendered = MappingTransformer().render(artifact, mapping, target_binding)
    expected = canonicalizer.observe({"serialized_space": rendered.serialized_space,
                                      "description": rendered.description})
    tgt_envelope = target.api_client.do(
        "GET", f"/api/2.0/genie/spaces/{target_binding.space_id}",
        query={"include_serialized_space": "true"})
    before = canonicalizer.observe(tgt_envelope)
    comparison = canonicalizer.compare(before, expected)
    if comparison != vc.Comparison.DIFFERENT:
        raise SystemExit(f"target must differ from rendered to force a write; got {comparison}")

    # 5. Upload the four files immutably as the source SP.
    prefix = f"{config['volumes']['outbound']}/sha256/{manifest.package_digest}"
    payloads = dict(files)
    payloads["manifest.json"] = packages.encode(manifest)
    from databricks.sdk.errors.platform import AlreadyExists

    for name, content in payloads.items():
        dest = f"{prefix}/{name}"
        try:
            source.files.upload(dest, BytesIO(content), overwrite=False)
        except AlreadyExists:
            # Content-addressed path: an identical prior upload is fine, a divergent
            # one is a digest collision we must never mask. Fall through to verify.
            pass
        readback = source.files.download(dest).contents.read()
        if readback != content:
            raise SystemExit(f"immutable upload mismatch for {name}")

    manifest_uri = f"{prefix}/manifest.json"
    config["manifest_uri"] = manifest_uri
    config["source_version_id"] = version_id
    config["mapping_digest"] = manifest.mapping_digest
    config["package_digest"] = manifest.package_digest
    Path(path).write_text(json.dumps(config, indent=2))
    print(f"package_digest={manifest.package_digest}")
    print(f"manifest_uri={manifest_uri}")
    print(f"source_version_id={version_id}")
    print(f"rendered vs target: {comparison}")


if __name__ == "__main__":
    main()
