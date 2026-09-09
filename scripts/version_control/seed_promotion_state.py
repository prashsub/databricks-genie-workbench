#!/usr/bin/env python3
"""D2.5a: seed the durable promotion state via the *real* M02/M03 write paths.

Before the governed ``vc-promotion`` Job can execute a promotion by operation id,
three durable facts must already exist in the provisioned control namespace:

1. **Registry** — the target binding must resolve as ``BOUND`` (a provisional
   ``enroll`` event followed by a ``bind_created`` event) so the gate can fence it.
2. **Coordination** — the single IDLE ``genie_ops_coordination`` row for that
   binding (created as a side effect of ``enroll`` -> the M03 initializer).
3. **Ledger** — the immutable source version the package manifest pins
   (``source_version_id`` + digests), imported into the target-owned
   ``genie_space_versions`` under the *target-workspace* ``source_binding``
   (``DeltaVersionLedger.append_observation`` only trusts its own workspace; the
   real source workspace is carried separately by ``source_workspace_id``).

This module drives the genuine ``DeltaRegistry`` / ``DeltaCoordinationStore`` /
``DeltaVersionLedger`` adapters (never owner SQL): ``enroll`` with an injected
``new_binding_id`` pinning the configured binding id, and ``append_observation``
with an injected ``new_version_id`` pinning the manifest's ``source_version_id``.
All three steps are idempotent, so a partially-completed seed is safely re-run.

Offline it is exercised with fake stores / a fake ``adapters`` (the same surface
as the Job's ``PlatformAdapters``); live it runs as the executor SP — the only
principal the least-privilege grant matrix authorizes to write the registry,
coordination and version fact tables — against ``fevm-serverless``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control import contracts as vc
from backend.services.version_control.coordination.store import DeltaCoordinationStore
from backend.services.version_control.ledger import DeltaVersionLedger
from backend.services.version_control.promotion import packages
from backend.services.version_control.registry import DeltaRegistry

_OBSERVATION_KEY = "promotion-source-import"
_BINDING_REASON = "vc-promotion-gate source import (D2.5a)"


def _digest(domain: str, payload: dict) -> vc.Digest:
    return vc.canonical_json_hash(domain, payload)


def _deterministic_uuid(*parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "/".join(("vc-seed", *parts))))


def build_seed_stores(config: dict, *, target_binding: vc.BindingRef, source_version_id: str,
                      adapters: Any = None, clock=None) -> SimpleNamespace:
    """Construct the real M02/M03 write-path adapters over the executor SQL seam.

    ``registry`` pins the configured binding id (``new_binding_id``) and routes
    enrollment initialization to ``coordination.initialize`` (the single IDLE row);
    ``ledger`` pins the manifest ``source_version_id`` (``new_version_id``). The
    executor SP is the sole grant-matrix writer for all three fact tables, so one
    SQL seam (``adapters.sql('executor')``) drives every write.
    """
    if adapters is None:
        from backend.services.version_control.platform.live_seams import (
            PlatformAdapters,
        )

        adapters = PlatformAdapters(config)
    clock = clock or (lambda: datetime.now(UTC))
    catalog, control_schema = config["catalog"], config["control_schema"]
    target_workspace_id = config["workspace_id"]
    sql = adapters.sql("executor")

    holder: dict[str, DeltaRegistry] = {}
    coordination = DeltaCoordinationStore(
        sql, f"`{catalog}`.`{control_schema}`.`genie_ops_coordination`",
        resolve_binding=lambda binding_id: holder["registry"].resolve(binding_id), clock=clock)
    registry = DeltaRegistry(
        sql, catalog, control_schema, target_workspace_id, clock=clock, writes_enabled=True,
        initialize=coordination.initialize, new_binding_id=lambda: target_binding.binding_id)
    holder["registry"] = registry
    ledger = DeltaVersionLedger(
        sql, catalog, control_schema, target_workspace_id, writes_enabled=True,
        new_version_id=lambda: source_version_id)
    return SimpleNamespace(registry=registry, coordination=coordination, ledger=ledger)


def _enrollment_request(target_binding: vc.BindingRef) -> vc.EnrollmentRequest:
    # `enroll` always records a provisional (unbound) head, so `space_id` is None
    # here; `bind_created` attaches the physical id below.
    return vc.EnrollmentRequest(target_binding.space_key, target_binding.workspace_id, None,
                                target_binding.environment, _BINDING_REASON)


def _create_intent(target_binding: vc.BindingRef, operation_id: str) -> vc.CreateIntentRef:
    event_id = _deterministic_uuid("create-intent", target_binding.binding_id)
    request_digest = _digest("vc-create-intent/1", {
        "binding_id": target_binding.binding_id, "space_id": target_binding.space_id,
        "operation_id": operation_id})
    return vc.CreateIntentRef(event_id, operation_id, target_binding.binding_id,
                              target_binding.binding_revision, request_digest)


def _identity_evidence(target_binding: vc.BindingRef, actor: vc.ActorContext,
                       now: datetime) -> vc.IdentityEvidence:
    evidence_digest = _digest("vc-identity-evidence/1", {
        "space_id": target_binding.space_id, "workspace_id": target_binding.workspace_id,
        "subject_id": actor.subject_id})
    return vc.IdentityEvidence("VC/1.0", target_binding.workspace_id, target_binding.space_id,
                               evidence_digest, now, actor)


def verify_source(snapshot: vc.Snapshot, manifest: vc.PackageManifest) -> None:
    """Fail closed unless the freshly captured source matches the pinned package.

    The Job's ``_inputs`` re-derives exactly these values from the ledger row and
    refuses on any mismatch, so a drifted source space is caught here (rebuild the
    package) rather than surfacing as an opaque Job failure.
    """
    mismatches = []
    if snapshot.raw_state_digest != manifest.raw_source_digest:
        mismatches.append("raw_source_digest")
    if snapshot.fingerprints != manifest.source_fingerprints:
        mismatches.append("source_fingerprints")
    if packages.digest(packages.portable(snapshot)) != manifest.artifact_digest:
        mismatches.append("artifact_digest")
    if mismatches:
        raise ValueError(
            "Captured source no longer matches the pinned package "
            f"(mismatched: {mismatches}); rebuild the promotion package (D2.3).")


def seed(stores: SimpleNamespace, *, target_binding: vc.BindingRef,
         source_binding: vc.BindingRef, capture_envelope, canonicalizer,
         manifest: vc.PackageManifest, actor: vc.ActorContext, now: datetime,
         operation_id: str | None = None) -> dict:
    """Idempotently seed registry (bound), coordination (IDLE) and ledger (source).

    Re-entrant: an already-bound registry, an existing coordination row, and a
    prior identical ledger import are each detected and left untouched.
    """
    operation_id = operation_id or _deterministic_uuid("enroll-operation", target_binding.binding_id)
    result: dict[str, Any] = {}

    # 1. Registry: drive to BOUND. `resolve` raises KeyError for a fresh binding;
    #    a provisional head (partial prior run) resolves with space_id=None.
    try:
        current = stores.registry.resolve(target_binding.binding_id)
    except KeyError:
        current = None
    if current == target_binding:
        result["registry"] = "already-bound"
    else:
        if current is None:
            stores.registry.enroll(_enrollment_request(target_binding), actor)
            result["registry"] = "enrolled+bound"
        else:
            result["registry"] = "bound"  # provisional head already present
        bound = stores.registry.bind_created(
            _create_intent(target_binding, operation_id), target_binding.space_id,
            _identity_evidence(target_binding, actor, now))
        if bound != target_binding:
            raise ValueError(f"bind_created produced {bound!r}, expected {target_binding!r}")

    # 2. Coordination: `enroll` creates the single IDLE row via the M03 initializer.
    row = stores.coordination.read(target_binding.binding_id)
    if row is None:
        raise ValueError("Coordination row missing after enrollment; initializer did not run")
    result["coordination_state"] = row.state.value

    # 3. Ledger: import the immutable source version (idempotent on observation_key).
    envelope = capture_envelope()
    snapshot = canonicalizer.observe(envelope)
    verify_source(snapshot, manifest)
    context = vc.CaptureContext(source_binding, _OBSERVATION_KEY, now, "promotion-source-import",
                               actor, vc.Origin.WORKBENCH)
    reference = stores.ledger.append_observation(snapshot, context)
    if reference.version_id != manifest.source_version_id:
        raise ValueError(
            f"Ledger version_id {reference.version_id} != manifest "
            f"source_version_id {manifest.source_version_id}")
    result["source_version_id"] = reference.version_id
    return result


def _load_manifest(adapters: Any, manifest_uri: str) -> vc.PackageManifest:
    raw = adapters.client("executor").files.download(manifest_uri).contents.read()
    return vc.from_wire(vc.PackageManifest, json.loads(raw))


def main(argv=None) -> None:
    from databricks.sdk import WorkspaceClient  # noqa: F401 - ensure SDK present

    from backend.services.config_fingerprint import Canonicalizer
    from backend.services.version_control.platform.live_seams import PlatformAdapters
    from scripts.version_control.author_governed_config import author

    two_ws_path = os.environ.get("VC_TWO_WORKSPACE_CONFIG")
    if not two_ws_path:
        raise SystemExit("VC_TWO_WORKSPACE_CONFIG must point to the two-workspace config")
    cfg = json.loads(Path(two_ws_path).read_text())
    governed = author(cfg, promotion_job_id=cfg.get("promotion_job_id"))
    # Persist the derived source_binding so the seed and the in-Job governed config
    # agree byte-for-byte (the Job's `_inputs` requires the ledger row to equal it).
    if cfg.get("source_binding") != governed["source_binding"]:
        cfg["source_binding"] = governed["source_binding"]
        Path(two_ws_path).write_text(json.dumps(cfg, indent=2))

    target_binding = vc.from_wire(vc.BindingRef, cfg["target_binding"])
    source_binding = vc.from_wire(vc.BindingRef, governed["source_binding"])
    adapters = PlatformAdapters(governed)
    manifest = _load_manifest(adapters, cfg["manifest_uri"])
    if manifest.source_version_id != cfg["source_version_id"]:
        raise SystemExit("manifest source_version_id disagrees with two-workspace config")

    def capture_envelope():
        client = adapters.client("source")
        return client.api_client.do(
            "GET", f"/api/2.0/genie/spaces/{source_binding.space_id}",
            query={"include_serialized_space": "true"})

    stores = build_seed_stores(governed, target_binding=target_binding,
                               source_version_id=manifest.source_version_id, adapters=adapters)
    actor = vc.ActorContext(governed["roles"]["executor"]["principal_id"],
                            governed["workspace_id"], "service")
    result = seed(stores, target_binding=target_binding, source_binding=source_binding,
                  capture_envelope=capture_envelope, canonicalizer=Canonicalizer(),
                  manifest=manifest, actor=actor, now=datetime.now(UTC),
                  operation_id=cfg.get("operation_id"))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
