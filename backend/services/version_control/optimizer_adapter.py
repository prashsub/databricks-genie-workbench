"""Target-local M10 seam. Composition supplies durable VC/1.0 implementations."""

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from backend.services.version_control import contracts as vc


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


class ChampionSources(Protocol):
    def load(self, run_id: str, champion_id: str) -> ChampionArtifact:
        """Read immutable, finalized source; never authorize via telemetry fallback."""
        ...


class ChampionRequests(Protocol):
    def prepare(self, request: ChampionMutationRequest, requester_id: str) -> ChampionMutationRequest:
        """Durably register the immutable request before the mutation gate."""
        ...


class WriteFlags(Protocol):
    def enabled(self, name: str) -> bool: ...


class OptimizerChampionAdapter:
    def __init__(self, *, sources: ChampionSources, gate: vc.MutationGate,
                 requests: ChampionRequests, flags: WriteFlags | None = None):
        self.sources = sources
        self.gate = gate
        self.requests = requests
        self.flags = flags

    def apply(self, run_id, champion_id, binding, expected_base, executor):
        if self.flags is None or any(self.flags.enabled(name) is not True for name in (
            "vc_writes_enabled", "vc_optimizer_apply_enabled", "vc_optimizer_contract_ready",
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
        digest = vc.canonical_json_hash("vc-optimizer-request/1", vc.to_wire(source))
        request = ChampionMutationRequest(
            vc.RequestIdentity(str(uuid4()), str(uuid4()), digest), binding, "optimizer_apply",
            source.source_version_id, expected_base, source.serialized_space, source.description,
            source.approval_id, vc.Origin.OPTIMIZER, run_id, champion_id, source.payload_digest,
        )
        request = self.requests.prepare(request, source.requester_id)
        return self.gate.execute(request, executor)
