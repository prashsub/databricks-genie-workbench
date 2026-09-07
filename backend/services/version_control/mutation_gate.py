"""Fail-closed orchestration over VC/1.0 ports."""

from datetime import datetime, timezone
from uuid import uuid4

from backend.services.version_control import contracts as vc


class MutationGate:
    def __init__(self, *, coordination, ledger, facts, approvals, transport,
                 canonicalizer, flags, registry=None):
        self.coordination = coordination
        self.ledger = ledger
        self.facts = facts
        self.approvals = approvals
        self.transport = transport
        self.canonicalizer = canonicalizer
        self.flags = flags
        self.registry = registry

    def execute(self, request, executor):
        reservation = self.coordination.reserve(request.binding, request.identity, executor)
        snapshot = self.canonicalizer.observe(self.transport.get(request.binding, executor))
        preimage = self._capture(request, executor, reservation.fence, snapshot, "preimage")
        authorization = self.approvals.authorize(request, executor)
        claim = self.coordination.admit(reservation, preimage, authorization)
        desired = self.canonicalizer.observe({
            **vc.to_wire(snapshot.response_envelope),
            "serialized_space": vc.to_wire(request.serialized_space),
            **({"description": request.description} if request.description is not None else {})})
        if self.canonicalizer.compare(snapshot, desired) == vc.Comparison.EQUAL:
            return self._finish(request, executor, claim, vc.OperationStatus.NOOP, preimage)
        self.coordination.assert_owner(claim)
        self.transport.patch_config_once(request.binding, vc.to_wire(request.serialized_space), claim)

    def _finish(self, request, executor, claim, status, postimage=None):
        evidence = vc.StageEvidence("VC/1.0", vc.PatchStage.CONFIG_OBSERVED,
                                    datetime.now(timezone.utc),
                                    postimage.state_digest if postimage else request.identity.request_digest,
                                    postimage)
        self.facts.append(vc.OperationFact(
            str(uuid4()), vc.FactKind.OPERATION, request.identity.operation_id, 100,
            f"{claim.attempt_id}:{status.value}", request.binding, request.identity,
            request.operation_type, executor.principal_id,
            vc.ActorContext(executor.principal_id, executor.workspace_id, executor.actor_kind),
            vc.FactStatus(status.value), evidence, datetime.now(timezone.utc),
            attempt_id=claim.attempt_id, generation=claim.generation,
            pre_version_id=claim.preimage.version_id,
            post_version_id=postimage.version_id if postimage else None))
        result = vc.OperationResult(request.identity.operation_id, status, claim.preimage,
                                    postimage, False, ())
        self.coordination.finish(claim, result)
        return result

    def _capture(self, request, executor, fence, snapshot, reason, parent=None):
        context = vc.CaptureContext(
            request.binding, f"{fence.attempt_id}:{reason}", datetime.now(timezone.utc), reason,
            vc.ActorContext(executor.principal_id, executor.workspace_id, executor.actor_kind),
            vc.Origin.EXTERNAL if reason == "preimage" else vc.Origin.WORKBENCH,
            parent_version_id=parent, operation_id=request.identity.operation_id,
            attempt_id=fence.attempt_id, generation=fence.generation)
        observation = self.ledger.append_observation(snapshot, context)
        if not self.ledger.verify_committed(observation):
            raise RuntimeError("Evidence was not committed")
        return observation
