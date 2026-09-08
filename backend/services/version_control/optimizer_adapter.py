"""Target-local M10 seam. Composition supplies durable VC/1.0 implementations."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control import contracts as vc
from backend.services.version_control.governance.approvals import request_digest
from backend.services.version_control.governance.facts import DurableOperationFacts


def source_digest(serialized_space, description):
    return vc.canonical_json_hash("vc-optimizer-source/1", {
        "serialized_space": serialized_space, "description": description,
    })


@dataclass(frozen=True)
class ChampionArtifact(vc.WireValue):
    run_id: str
    champion_id: str
    binding: vc.BindingRef
    expected_base: vc.Digest
    source_version_id: vc.UUIDString
    serialized_space: vc.JsonObject
    description: str | None
    payload_digest: vc.Digest
    requester_id: str
    approval_id: vc.UUIDString | None


@dataclass(frozen=True)
class ChampionMutationRequest(vc.MutationRequest):
    origin: vc.Origin
    optimizer_run_id: str
    champion_id: str
    source_payload_digest: vc.Digest


class ChampionOperationFacts(DurableOperationFacts):
    """Target-local durable facts with the M10 champion request wire extension."""

    def get_request(self, operation_id):
        rows = self._read()
        base_fields = vc.MutationRequest.__dataclass_fields__
        base_rows = [dict(row, operation_request={key: value for key, value in row["operation_request"].items()
                                                 if key in base_fields})
                     if ("operation_request" in row
                         and row["operation_request"].get("operation_type") == "optimizer_apply")
                     else row for row in rows]
        projected = DurableOperationFacts(lambda: base_rows, self._insert).get_request(operation_id)
        if projected.request.operation_type != "optimizer_apply":
            return projected
        requests = [vc.from_wire(ChampionMutationRequest, row["operation_request"])
                    for row in rows if row["operation_id"] == operation_id and "operation_request" in row]
        if any(request != requests[0] for request in requests):
            raise ValueError("Ambiguous durable champion request")
        return vc.ApprovedOperation(requests[0], projected.approval)


class ChampionSources(Protocol):
    def load(self, run_id: str, champion_id: str) -> ChampionArtifact:
        """Read immutable, finalized source; never authorize via telemetry fallback."""
        ...


class ChampionRequests(Protocol):
    def prepare(self, request: ChampionMutationRequest, requester_id: str) -> ChampionMutationRequest:
        """Atomically put-if-absent by run/binding-revision key, never overwrite.

        Return the original request on exact retry; reject different digests.
        Retain the key after coordination reuse and process/Job termination.
        Ambiguous persistence must raise, never return optimistic success.
        """
        ...


class WriteFlags(Protocol):
    def enabled(self, name: str) -> bool: ...


class OptimizerChampionAdapter:
    def __init__(self, *, sources: ChampionSources, gate: vc.MutationGate,
                 requests: ChampionRequests, flags: WriteFlags | None = None,
                 facts: vc.OperationFacts | None = None,
                 identity: vc.IdentityProvider | None = None,
                 approvals: vc.ApprovalService | None = None):
        self.sources = sources
        self.gate = gate
        self.requests = requests
        self.flags = flags
        self.facts = facts
        self.identity = identity
        self.approvals = approvals

    def apply(self, run_id, champion_id, binding, expected_base, executor):
        if self.flags is None or any(self.flags.enabled(name) is not True for name in (
            "vc_writes_enabled", "vc_optimizer_apply_enabled",
        )):
            raise PermissionError("Optimizer champion writes are disabled")
        if (executor.workspace_id != binding.workspace_id or executor.actor_kind != "service"
                or not executor.execution_ref.startswith("job/")):
            raise PermissionError("Champion apply requires a target-local Job executor")
        source = self.sources.load(run_id, champion_id)
        if (source.run_id != run_id or source.champion_id != champion_id
                or source.binding != binding or source.expected_base != expected_base):
            raise PermissionError("Champion source identity/base does not match request")
        if source.payload_digest != source_digest(source.serialized_space, source.description):
            raise ValueError("Champion source payload digest mismatch")
        if self.identity is None or self.approvals is None:
            raise PermissionError("Requester identity and M06 authorization ports are required")
        self.identity.verify_run_as(executor.execution_ref, executor.principal_id)
        if not source.requester_id:
            raise PermissionError("Requester must be durably bound to the source")
        if source.approval_id is None:
            raise PermissionError("Requester must supply an M06 approval for optimizer champion apply")
        key = vc.canonical_json_hash("vc-optimizer-run/1", {
            "run_id": run_id, "binding_id": binding.binding_id,
            "binding_revision": binding.binding_revision, "workspace_id": binding.workspace_id,
        })
        request = ChampionMutationRequest(
            vc.RequestIdentity(str(uuid5(NAMESPACE_URL, "vc-optimizer-run/1:" + key)), key, "0" * 64),
            binding, "optimizer_apply",
            source.source_version_id, expected_base, source.serialized_space, source.description,
            source.approval_id, vc.Origin.OPTIMIZER, run_id, champion_id, source.payload_digest,
        )
        request = replace(request, identity=replace(request.identity, request_digest=request_digest(request)))
        persisted = self.requests.prepare(request, source.requester_id)
        if persisted != request:
            raise ValueError("Run already has a different champion/digest")
        if self.facts is None:
            raise PermissionError("Authoritative operation facts are required")
        if self.facts.get_request(request.identity.operation_id).request != request:
            raise PermissionError("Champion request is not durably published")
        grant = self.approvals.authorize(request, executor)
        if (not isinstance(grant, vc.AuthorizationGrant) or grant.binding != binding
                or grant.request != request.identity or not grant.authorization_reference
                or grant.expires_at <= datetime.now(timezone.utc)
                or grant.expected_base_fingerprints.state_digest != expected_base
                or grant.rendered_target_digest != vc.canonical_json_hash("vc-rendered-target/1", {
                    "serialized_space": request.serialized_space, "description": request.description,
                })
                or grant.approval_id != source.approval_id):
            raise PermissionError("M06 grant is stale or not bound to champion request")
        return self.gate.execute(request, executor)
