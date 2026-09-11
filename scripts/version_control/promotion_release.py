#!/usr/bin/env python3
"""Operator release surface for the governed two-workspace promotion (D2.5b).

`build_release_surface(config, adapters=...)` assembles the *same* live governed
leaf graph as `backend.jobs.resolve_governed_ports` (facts -> ledger/registry ->
coordination -> approvals/transport -> gate -> promotion) but keeps references to
every leaf, then binds a `ReleaseCommands` over the real `PromotionService` with
`facts.record_request` as the durable request writer. This is the *operator*
command surface (create -> validate -> approve -> promote) that mints the
approved operation fact the ``vc-promotion`` Job later executes by operation id.

Offline it is exercised with a fake `adapters` (same surface as the Job's
`PlatformAdapters`); live it drives the real approval workflow as the target /
approval principals. It is deliberately a thin composition + driver, never a
second policy implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from backend.services.version_control import contracts as vc
from backend.services.version_control.governance.approvals import approval_digest
from backend.services.version_control.platform.feature_flags import FeatureFlags
from backend.services.version_control.promotion.packages import (
    mapping_digest,
    policy_digests,
)
from backend.services.version_control.promotion.releases import ReleaseCommands


def build_release_surface(config: dict, *, adapters: Any = None) -> SimpleNamespace:
    """Assemble the live governed graph and the operator ReleaseCommands surface.

    Mirrors `resolve_governed_ports`' dependency order exactly (kept in lockstep
    by the offline composition test) but returns the individual leaves so the
    operator flow can drive approvals and read facts directly.
    """
    from backend.services.version_control.platform.live_seams import (
        PlatformAdapters,
        build_governed_seams,
    )

    if adapters is None:
        adapters = PlatformAdapters(config)
    seams = build_governed_seams(config, adapters=adapters)
    flags = FeatureFlags.from_config(config.get("flags", {}))

    facts = seams.facts(config)
    ledger = seams.ledger(config)
    registry = seams.registry(config)
    canonicalizer = seams.canonicalizer(config)
    identity = seams.identity(config)
    executor = seams.executor(config, identity=identity)
    coordination = seams.coordination(
        config, facts=facts, ledger=ledger, registry=registry, canonicalizer=canonicalizer)
    approvals = seams.approvals(config, facts=facts, identity=identity, registry=registry)
    transport = seams.transport(
        config, executor=executor, coordination=coordination, registry=registry, flags=flags)
    gate = seams.gate(
        coordination=coordination, ledger=ledger, facts=facts, approvals=approvals,
        transport=transport, canonicalizer=canonicalizer, flags=flags, registry=registry)
    dispatcher = seams.dispatcher(config, identity=identity, executor=executor, facts=facts)
    promotion = seams.promotion(
        config, facts=facts, gate=gate, approvals=approvals, identity=identity, ledger=ledger,
        canonicalizer=canonicalizer, registry=registry, transport=transport, flags=flags,
        executor=executor, dispatcher=dispatcher)

    commands = ReleaseCommands(promotion=promotion, request_writer=facts.record_request,
                               authorize_history=lambda actor, binding: True)
    return SimpleNamespace(
        commands=commands, promotion=promotion, facts=facts, approvals=approvals,
        registry=registry, ledger=ledger, coordination=coordination, identity=identity,
        executor=executor, canonicalizer=canonicalizer, flags=flags)


def build_approval_inputs(surface: SimpleNamespace, request, mapping, policy, manifest,
                          evidence, base_fingerprints, requester_id, *, ttl_hours=6) -> vc.ApprovalInputs:
    """Derive the immutable approval inputs from the released request + preflight.

    The digests come from the released package manifest and the freshly recomputed
    preflight `evidence`, so the values the Job's `execute` re-derives at run time
    match verbatim (see `PromotionService.execute`'s `expected` map).
    """
    rendered = vc.canonical_json_hash('vc-rendered-target/1', {
        'serialized_space': request.serialized_space, 'description': request.description})
    return vc.ApprovalInputs(
        'VC/1.0', request.identity.operation_id, 'promotion', request.source_version_id,
        manifest.raw_source_digest, manifest.source_fingerprints,
        manifest.artifact_digest, mapping_digest(mapping), rendered,
        mapping.transformer_version, 'vc-c14n/1', request.binding, base_fingerprints,
        evidence.permission_policy_digest, policy_digests(policy)[0], policy_digests(policy)[1],
        evidence.preflight_evidence_digest, policy.thresholds, requester_id, {},
        datetime.now(UTC) + timedelta(hours=ttl_hours))


def approve_record(inputs: vc.ApprovalInputs, approver_ids) -> vc.ApprovalRecord:
    """Build an APPROVED record with one vote per approver (offline helper)."""
    votes = tuple(vc.ApprovalVote(approver, 'approve', datetime.now(UTC)) for approver in approver_ids)
    return vc.ApprovalRecord(vc.ApprovalRequest(inputs.operation_id, inputs, datetime.now(UTC)),
                             votes, vc.FactStatus.APPROVED, approval_digest(inputs, votes))
