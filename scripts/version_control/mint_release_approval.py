#!/usr/bin/env python3
"""D2.5b: mint the approved operation fact via the *real* release/approval flow.

The governed ``vc-promotion`` Job executes a promotion **by operation id**: it
looks up an already-approved OPERATION fact (``CONFIG_PENDING`` stage, sequence 1)
and replays the target write. That fact only exists after a genuine target-local
release + two-of-N human approval workflow has run. This driver executes exactly
that workflow against the provisioned namespace, minting the fact the Job later
consumes -- it never fabricates approvals or writes owner SQL.

Flow (all over the live governed leaf graph from ``build_release_surface``):

1. **base pre-observe** -- one ``observer.capture`` to commit the target's current
   serialized space as the observed head. M07 ``preflight`` re-observes and, on an
   unchanged target, gets that head back as ``captured_version`` with a CLEAN
   status reader (see the M04 unchanged-capture fix); ``expected_base`` is pinned
   to the head's ``state_digest`` so ``create`` and ``preflight`` agree.
2. **create** -- writes the immutable RELEASE fact + target-local request
   (requester = a release-requester SP with edit on the target space).
3. **validate** -- runs ``preflight`` and appends the ``CONFIG_OBSERVED`` stage.
4. **request + vote x2** -- the real ``ApprovalService``: a requester APPROVAL_REQUEST
   then two distinct non-requester human approve votes (live SCIM group checks).
5. **authorize** -- the target service executor publishes the APPROVED grant
   (quorum + target-approver membership + ``run_as`` all verified live).
6. **promote** -- mints the ``CONFIG_PENDING`` approved OPERATION fact (sequence 1).

Offline it is exercised with a fake release surface that pins the ordered
orchestration contract (below); the real leaf graph + real approval/preflight
logic are covered by ``test_vc_promotion_release_surface`` and the M07 suites.
Live it runs as the target/approval principals against ``fevm-serverless``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control import contracts as vc
from scripts.version_control.promotion_release import build_approval_inputs


def _release_operation_id(actor: vc.ActorContext, key: str) -> str:
    """The deterministic release operation id ReleaseCommands.create derives from
    (workspace, actor, key). Recomputed here so the driver is resumable: if a prior
    run already released this exact operation, we skip create (whose RELEASE fact is
    immutable and would otherwise conflict on a fresh recorded_at) and resume."""
    return str(uuid5(NAMESPACE_URL, vc.canonical_json_hash('vc-release-id/1', {
        'workspace': actor.workspace_id, 'actor': actor.subject_id, 'key': key})))


@dataclass(frozen=True)
class MintPlan:
    """Immutable inputs for a single release+approval mint.

    ``requester`` is the release-requester (service) actor; ``approvers`` are the
    two distinct non-requester human actors. ``mapping``/``policy`` must be the
    exact objects the promotion package (D2.3) was built from so ``create``'s
    rendered request and ``build_approval_inputs``' digests match the manifest.
    """

    target_binding: vc.BindingRef
    source_version_id: str
    mapping: Any
    policy: Any
    source_profile: str
    target_profile: str
    key: str
    requester: vc.ActorContext
    approvers: tuple[vc.ActorContext, ...]
    ttl_hours: int = 6


def _release_manifest(surface: SimpleNamespace, operation_id: str) -> vc.PackageManifest:
    """Load the pinned package manifest for the release (same read the Job uses)."""
    release = surface.promotion.releases.get(operation_id)
    raw = surface.promotion.store.read(release.package.manifest_uri)
    return vc.from_wire(vc.PackageManifest, json.loads(raw))


def mint(surface: SimpleNamespace, plan: MintPlan, *,
         clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> dict:
    """Drive create -> validate -> request -> vote x2 -> authorize -> promote.

    Returns the minted ``operation_id`` (== ``approval_id``) and the pinned
    ``expected_base`` so the caller can assert the durable fact and hand the id to
    the Job. Fails closed on any missing base observation; every downstream step
    raises through the real services (no swallowed errors)."""
    executor = surface.identity.executor(surface.promotion.target_selection)

    # 1. base pre-observe: commit / confirm the observed head and pin expected_base.
    base = surface.promotion.observer.capture(
        plan.target_binding, "promotion_base_observe", executor)
    if base.busy or base.status.stale or base.captured_version is None:
        raise RuntimeError("Base observation unavailable; cannot pin promotion base")
    expected_base = base.captured_version.fingerprints.state_digest

    # 2. create the immutable release + target-local request (resumable: the RELEASE
    #    fact is immutable, so if a prior run already released this deterministic
    #    operation id, reuse it instead of re-creating with a fresh recorded_at).
    operation_id = _release_operation_id(plan.requester, plan.key)
    try:
        surface.promotion.releases.get(operation_id)
    except LookupError:
        handle = surface.commands.create(
            plan.source_version_id, plan.mapping, plan.policy, expected_base,
            plan.source_profile, plan.target_profile, plan.key, plan.requester)
        operation_id = handle.operation_id

    # 3. validate: preflight + CONFIG_OBSERVED stage.
    surface.commands.validate(operation_id, plan.key, plan.requester)

    # 4. derive the immutable approval inputs from the released package + preflight.
    request = surface.facts.get_request(operation_id).request
    evidence = surface.promotion.preflight(operation_id, executor)
    manifest = _release_manifest(surface, operation_id)
    inputs = build_approval_inputs(
        surface, request, plan.mapping, plan.policy, manifest, evidence,
        evidence.expected_base_fingerprints, plan.requester.subject_id, ttl_hours=plan.ttl_hours)

    # 5. real approval workflow: request, two human approve votes, publish grant.
    surface.approvals.request(inputs, plan.requester)
    for approver in plan.approvers:
        surface.approvals.vote(operation_id, "approve", approver)
    surface.approvals.authorize(request, executor)

    # 6. promote: mint the CONFIG_PENDING approved OPERATION fact (sequence 1).
    surface.commands.promote(operation_id, operation_id, plan.key, plan.requester)
    return {"operation_id": operation_id, "approval_id": operation_id,
            "expected_base": expected_base}


def _plan_from_config(surface: SimpleNamespace, cfg: dict, governed: dict,
                      approval_identities: dict) -> MintPlan:
    """Assemble the MintPlan from the two-workspace + governed + approval configs.

    ``approval_identities`` (from ``VC_INTEGRATION_CONFIG``) carries the requester
    SP application id and the two human approver SCIM ids provisioned for the audit
    gate. The mapping/policy are re-derived from the shared ``build_mapping_policy``
    (the single source of truth D2.3 used) and verified byte-for-byte against the
    pinned ``mapping_digest`` so the release request matches the manifest exactly."""
    from scripts.version_control.build_promotion_package import build_mapping_policy

    target_binding = vc.from_wire(vc.BindingRef, cfg["target_binding"])
    workspace_id = governed["workspace_id"]
    mapping, policy = build_mapping_policy(cfg)
    if mapping.mapping_digest != cfg["mapping_digest"]:
        raise SystemExit(
            f"Re-derived mapping_digest {mapping.mapping_digest} != pinned "
            f"{cfg['mapping_digest']}; the package inputs drifted -- rebuild D2.3.")
    # Requester is the enrollment SP (application id); approvers are human SCIM ids.
    requester = vc.ActorContext(approval_identities["requester_principal_id"], workspace_id, "service")
    approvers = tuple(vc.ActorContext(a["id"], workspace_id, "human")
                      for a in approval_identities["approvers"])
    return MintPlan(
        target_binding=target_binding,
        source_version_id=cfg["source_version_id"],
        mapping=mapping, policy=policy,
        source_profile=surface.promotion.source_selection.profile,
        target_profile=surface.promotion.target_selection.profile,
        key=cfg.get("release_key", "vc-promotion-gate-d25b"),
        requester=requester, approvers=approvers)


def main(argv=None) -> None:
    import os
    from pathlib import Path

    from scripts.version_control.author_governed_config import author
    from scripts.version_control.promotion_release import build_release_surface

    two_ws_path = os.environ.get("VC_TWO_WORKSPACE_CONFIG")
    integration_path = os.environ.get("VC_INTEGRATION_CONFIG")
    if not two_ws_path or not integration_path:
        raise SystemExit("VC_TWO_WORKSPACE_CONFIG and VC_INTEGRATION_CONFIG must both be set")
    cfg = json.loads(Path(two_ws_path).read_text())
    approval_identities = json.loads(Path(integration_path).read_text())["approval_identities"]
    governed = author(cfg, promotion_job_id=cfg.get("promotion_job_id"))
    # The approval workflow (vote/authorize) must resolve human approver SCIM groups,
    # which the least-privileged executor SP cannot read. Point the identity provider's
    # directory client at the admin `directory_profile` for this local minting only
    # (the governed Job config is authored separately and never gains this role).
    directory_profile = approval_identities.get("directory_profile")
    if directory_profile:
        governed["roles"]["directory"] = {"profile": directory_profile}
        governed["directory_role"] = "directory"
    surface = build_release_surface(governed)
    plan = _plan_from_config(surface, cfg, governed, approval_identities)
    result = mint(surface, plan)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
