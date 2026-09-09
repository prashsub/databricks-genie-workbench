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

from backend.services.version_control import contracts as vc
from scripts.version_control.promotion_release import build_approval_inputs


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

    # 2. create the immutable release + target-local request.
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


def _actor(identity: dict, workspace_id: str, kind: str) -> vc.ActorContext:
    return vc.ActorContext(identity["principal_id"], workspace_id, kind)


def _plan_from_config(surface: SimpleNamespace, cfg: dict, governed: dict) -> MintPlan:
    """Assemble the MintPlan from the two-workspace + governed configs.

    The requester and human approver identities live under ``approval_identities``
    in the two-workspace config; the mapping/policy come from the package inputs
    persisted alongside it (byte-identical to D2.3)."""
    from backend.services.version_control.promotion import packages

    target_binding = vc.from_wire(vc.BindingRef, cfg["target_binding"])
    workspace_id = governed["workspace_id"]
    approval = cfg["approval_identities"]
    mapping = vc.from_wire(packages.PromotionMapping, cfg["mapping"])
    policy = vc.from_wire(packages.PromotionPolicy, cfg["policy"])
    return MintPlan(
        target_binding=target_binding,
        source_version_id=cfg["source_version_id"],
        mapping=mapping, policy=policy,
        source_profile=surface.promotion.source_selection.profile,
        target_profile=surface.promotion.target_selection.profile,
        key=cfg.get("release_key", "vc-promotion-gate-d25b"),
        requester=_actor(approval["requester"], workspace_id, "service"),
        approvers=tuple(_actor(a, workspace_id, "human") for a in approval["approvers"]),
    )


def main(argv=None) -> None:
    import os
    from pathlib import Path

    from scripts.version_control.author_governed_config import author
    from scripts.version_control.promotion_release import build_release_surface

    two_ws_path = os.environ.get("VC_TWO_WORKSPACE_CONFIG")
    if not two_ws_path:
        raise SystemExit("VC_TWO_WORKSPACE_CONFIG must point to the two-workspace config")
    cfg = json.loads(Path(two_ws_path).read_text())
    governed = author(cfg, promotion_job_id=cfg.get("promotion_job_id"))
    surface = build_release_surface(governed)
    plan = _plan_from_config(surface, cfg, governed)
    result = mint(surface, plan)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
