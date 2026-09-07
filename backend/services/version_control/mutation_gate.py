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
        if snapshot.state_digest != request.expected_base:
            self.coordination.quarantine(reservation.fence, "Reviewed base changed before admission")
            return vc.OperationResult(request.identity.operation_id, vc.OperationStatus.CONFLICTED,
                                      preimage, None, True, (preimage.version_id,))
        authorization = self.approvals.authorize(request, executor)
        claim = self.coordination.admit(reservation, preimage, authorization)
        desired = self.canonicalizer.observe({
            **vc.to_wire(snapshot.response_envelope),
            "serialized_space": vc.to_wire(request.serialized_space),
            **({"description": request.description} if request.description is not None else {})})
        if self.canonicalizer.compare(snapshot, desired) == vc.Comparison.EQUAL:
            return self._finish(request, executor, claim, vc.OperationStatus.NOOP, preimage)
        self.coordination.assert_owner(claim)
        try:
            self.transport.patch_config_once(request.binding, vc.to_wire(request.serialized_space), claim)
        except Exception:
            return self._unverified(request, executor, claim, "Config send outcome unknown")
        checkpoint = self.canonicalizer.observe(self.transport.get(request.binding, executor))
        middle = self._capture(request, executor, claim, checkpoint, "config_checkpoint", preimage.version_id)
        self._checkpoint(claim, vc.PatchStage.CONFIG_OBSERVED, middle)
        if (checkpoint.fingerprints.config != desired.fingerprints.config
                or checkpoint.fingerprints.benchmark != desired.fingerprints.benchmark
                or checkpoint.fingerprints.metadata != snapshot.fingerprints.metadata
                or checkpoint.fingerprints.canonicalizer_version != desired.fingerprints.canonicalizer_version):
            return self._partial(request, claim, middle, "Config or approved metadata checkpoint changed")
        if request.description is not None and snapshot.fingerprints.metadata != desired.fingerprints.metadata:
            self.coordination.assert_owner(claim)
            try:
                self.transport.patch_description_once(request.binding, request.description, claim)
            except Exception:
                return self._unverified(request, executor, claim, "Description send outcome unknown")
            final = self.canonicalizer.observe(self.transport.get(request.binding, executor))
            postimage = self._capture(request, executor, claim, final, "postimage", preimage.version_id)
            self._checkpoint(claim, vc.PatchStage.DESCRIPTION_OBSERVED, postimage)
            if self.canonicalizer.compare(final, desired) != vc.Comparison.EQUAL:
                return self._partial(request, claim, postimage, "Final state differs from desired")
        else:
            postimage = middle
        return self._finish(request, executor, claim, vc.OperationStatus.CONFIRMED, postimage)

    def _partial(self, request, claim, observation, reason):
        self.coordination.quarantine(claim, reason)
        return vc.OperationResult(request.identity.operation_id, vc.OperationStatus.APPLIED_PARTIAL,
                                  claim.preimage, observation, True, (reason,))

    def _unverified(self, request, executor, claim, reason):
        self.coordination.quarantine(claim, reason)
        self._record(request, executor, claim, vc.OperationStatus.APPLIED_UNVERIFIED)
        return vc.OperationResult(request.identity.operation_id, vc.OperationStatus.APPLIED_UNVERIFIED,
                                  claim.preimage, None, True, (reason,))

    def verify_only(self, operation_id, executor):
        request = self.facts.get_request(operation_id).request
        snapshot = self.canonicalizer.observe(self.transport.get(request.binding, executor))
        return vc.OperationResult(operation_id, vc.OperationStatus.APPLIED_UNVERIFIED,
                                  None, None, True, (snapshot.state_digest,))

    def _checkpoint(self, claim, stage, observation):
        self.coordination.checkpoint(claim, stage, vc.StageEvidence(
            "VC/1.0", stage, datetime.now(timezone.utc), observation.state_digest, observation))

    def _finish(self, request, executor, claim, status, postimage=None):
        self._record(request, executor, claim, status, postimage)
        result = vc.OperationResult(request.identity.operation_id, status, claim.preimage,
                                    postimage, False, ())
        self.coordination.finish(claim, result)
        return result

    def _record(self, request, executor, claim, status, postimage=None):
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
