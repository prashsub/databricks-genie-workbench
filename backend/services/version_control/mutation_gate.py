"""Fail-closed orchestration over VC/1.0 ports."""

from datetime import datetime, timezone
from dataclasses import replace
from uuid import uuid4

from backend.services.version_control import contracts as vc


class MutationGate:
    def __init__(self, *, coordination, ledger, facts, approvals, transport,
                 canonicalizer, flags=None, registry=None):
        self.coordination = coordination
        self.ledger = ledger
        self.facts = facts
        self.approvals = approvals
        self.transport = transport
        self.canonicalizer = canonicalizer
        self.flags = flags
        self.registry = registry

    def execute(self, request, executor):
        self._enabled(request.binding, executor, request.operation_type)
        history = self.facts.lookup_request(request.binding, request.identity.idempotency_key)
        if history.ambiguous or any(fact.request != request.identity for fact in history.facts):
            raise RuntimeError("Ambiguous or reused request identity")
        if history.facts:
            return self.verify_only(request.identity.operation_id, executor)
        reservation = self.coordination.reserve(request.binding, request.identity, executor)
        snapshot = self.canonicalizer.observe(self.transport.get(request.binding, executor))
        preimage = self._capture(request, executor, reservation.fence, snapshot, "preimage")
        if snapshot.state_digest != request.expected_base:
            claim = vc.AdmissionClaim(**vc.to_wire(reservation.fence), request=reservation.request,
                                      preimage=preimage, approval_id=None, approval_digest=None)
            return self._finish(request, executor, claim, vc.OperationStatus.CONFLICTED)
        authorization = self.approvals.authorize(request, executor)
        claim = self.coordination.admit(reservation, preimage, authorization)
        desired = self.canonicalizer.observe({
            **vc.to_wire(snapshot.response_envelope),
            "serialized_space": vc.to_wire(request.serialized_space),
            **({"description": request.description} if request.description is not None else {})})
        if self.canonicalizer.compare(snapshot, desired) == vc.Comparison.EQUAL:
            return self._finish(request, executor, claim, vc.OperationStatus.NOOP, preimage)
        self._checkpoint(claim, vc.PatchStage.CONFIG_IN_FLIGHT, None)
        self.coordination.assert_owner(claim)
        try:
            self.transport.patch_config_once(request.binding, vc.to_wire(request.serialized_space), claim)
        except Exception:
            return self._unverified(request, executor, claim, "Config send outcome unknown")
        try:
            checkpoint = self.canonicalizer.observe(self.transport.get(request.binding, executor))
            middle = self._capture(request, executor, claim, checkpoint, "config_checkpoint", preimage.version_id)
            self._checkpoint(claim, vc.PatchStage.CONFIG_OBSERVED, middle)
        except Exception:
            return self._unverified(request, executor, claim, "Config read-back evidence unavailable")
        if (checkpoint.fingerprints.config != desired.fingerprints.config
                or checkpoint.fingerprints.benchmark != desired.fingerprints.benchmark
                or checkpoint.fingerprints.metadata != snapshot.fingerprints.metadata
                or checkpoint.fingerprints.canonicalizer_version != desired.fingerprints.canonicalizer_version):
            return self._partial(request, executor, claim, middle, "Config or approved metadata checkpoint changed")
        if request.description is not None and snapshot.fingerprints.metadata != desired.fingerprints.metadata:
            self._checkpoint(claim, vc.PatchStage.DESCRIPTION_PENDING, middle)
            self._checkpoint(claim, vc.PatchStage.DESCRIPTION_IN_FLIGHT, None)
            self.coordination.assert_owner(claim)
            try:
                self.transport.patch_description_once(request.binding, request.description, claim)
            except Exception:
                return self._unverified(request, executor, claim, "Description send outcome unknown")
            try:
                final = self.canonicalizer.observe(self.transport.get(request.binding, executor))
                postimage = self._capture(request, executor, claim, final, "postimage", preimage.version_id)
                self._checkpoint(claim, vc.PatchStage.DESCRIPTION_OBSERVED, postimage)
            except Exception:
                return self._unverified(request, executor, claim, "Description read-back evidence unavailable")
            if self.canonicalizer.compare(final, desired) != vc.Comparison.EQUAL:
                return self._partial(request, executor, claim, postimage, "Final state differs from desired")
        else:
            postimage = middle
        try:
            return self._finish(request, executor, claim, vc.OperationStatus.CONFIRMED, postimage)
        except Exception:
            return self._unverified(request, executor, claim, "Final publication unavailable")

    def create(self, request, executor):
        self._enabled(request.binding, executor, "create")
        if request.binding.space_id is not None or self.registry.resolve(request.binding.binding_id) != request.binding:
            raise PermissionError("A durable provisional logical binding is required")
        if self.facts.get_request(request.identity.operation_id).request != request:
            raise PermissionError("Create request must be durable and immutable")
        history = self.facts.lookup_request(request.binding, request.identity.idempotency_key)
        if history.ambiguous or any(fact.request != request.identity for fact in history.facts):
            raise PermissionError("Create identity is ambiguous or reused")
        if history.facts:
            return vc.OperationResult(request.identity.operation_id, vc.OperationStatus.APPLIED_UNVERIFIED,
                                      None, None, True, ("Audited orphan resolution required; never replay create",))
        intent = vc.CreateIntentRef(str(uuid4()), request.identity.operation_id,
            request.binding.binding_id, request.binding.binding_revision, request.identity.request_digest)
        actor = vc.ActorContext(executor.principal_id, executor.workspace_id, executor.actor_kind)
        self.facts.append(vc.OperationFact(intent.event_id, vc.FactKind.CREATE_INTENT,
            request.identity.operation_id, 0, f"{request.identity.operation_id}:create-intent",
            request.binding, request.identity, "create", executor.principal_id, actor,
            vc.FactStatus.REQUESTED, intent, datetime.now(timezone.utc)))
        committed = self.facts.lookup_request(request.binding, request.identity.idempotency_key)
        if committed.ambiguous or not any(fact.evidence == intent for fact in committed.facts):
            raise RuntimeError("Create intent was not durably committed")
        reservation = self.coordination.reserve(request.binding, request.identity, executor)
        empty = self.canonicalizer.observe({"serialized_space": {}})
        mutation = vc.MutationRequest(request.identity, request.binding, "create", intent.event_id,
            empty.state_digest, request.payload.serialized_space, request.payload.description, request.approval_id)
        grant = self.approvals.authorize(mutation, executor)
        claim = self.coordination.admit(reservation, intent, grant)
        self._checkpoint(claim, vc.PatchStage.CONFIG_IN_FLIGHT, None)
        self.coordination.assert_owner(claim)
        try:
            response = self.transport.create_once(request.payload, claim)
            if not response.space_id:
                raise RuntimeError("Create response has no physical identity")
            evidence = vc.IdentityEvidence("VC/1.0", request.binding.workspace_id, response.space_id,
                vc.canonical_json_hash("vc-create-response/1", response.response_envelope),
                datetime.now(timezone.utc), actor)
            physical = self.registry.bind_created(intent, response.space_id, evidence)
            if (physical != replace(request.binding, space_id=response.space_id)
                    or self.registry.resolve(physical.binding_id) != physical):
                raise RuntimeError("Physical binding was not durably resolved")
            mutation = replace(mutation, binding=physical)
            snapshot = self.canonicalizer.observe(self.transport.get(physical, executor))
            postimage = self._capture(mutation, executor, claim, snapshot, "postimage")
            self._checkpoint(claim, vc.PatchStage.CONFIG_OBSERVED, postimage)
            desired = self.canonicalizer.observe({"serialized_space": vc.to_wire(request.payload.serialized_space),
                **({"description": request.payload.description} if request.payload.description is not None else {})})
            if self.canonicalizer.compare(snapshot, desired) != vc.Comparison.EQUAL:
                return self._partial(mutation, executor, claim, postimage, "Created state differs from approved payload")
            return self._finish(mutation, executor, claim, vc.OperationStatus.CONFIRMED, postimage)
        except Exception:
            return self._unverified(mutation, executor, claim, "Possible create orphan; audited identity resolution required")

    def _enabled(self, binding, executor, operation_type):
        if self.flags is None or self.flags.enabled("vc_writes_enabled") is not True:
            raise PermissionError("VC writes disabled")
        switches = {"restore": "vc_restore_enabled", "promotion": "vc_promotion_enabled",
                    "optimizer_apply": "vc_optimizer_apply_enabled", "reapply": "vc_reconcile_enabled"}
        if operation_type not in {"edit", "create", *switches}:
            raise PermissionError("Unknown mutation kind disabled")
        if operation_type in switches or binding.environment == "prod":
            if (not executor.execution_ref.startswith("job/") or executor.actor_kind != "service"
                    or (operation_type in switches and self.flags.enabled(switches[operation_type]) is not True)):
                raise PermissionError("Governed writes disabled outside target-local Jobs")
        if executor.workspace_id != binding.workspace_id:
            raise PermissionError("Cross-workspace mutation disabled")

    def _partial(self, request, executor, claim, observation, reason):
        self._record(request, executor, claim, vc.OperationStatus.APPLIED_PARTIAL, observation)
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
            "VC/1.0", stage, datetime.now(timezone.utc),
            observation.state_digest if observation else claim.request.request_digest, observation))

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
            pre_version_id=claim.preimage.version_id if isinstance(claim.preimage, vc.ObservationRef) else None,
            post_version_id=postimage.version_id if postimage else None))

    def _capture(self, request, executor, fence, snapshot, reason, parent=None):
        context = vc.CaptureContext(
            request.binding, f"{fence.attempt_id}:{reason}", datetime.now(timezone.utc), reason,
            vc.ActorContext(executor.principal_id, executor.workspace_id, executor.actor_kind),
            vc.Origin.EXTERNAL if reason == "preimage" else (
                vc.Origin.RESTORE if request.operation_type == "restore" else vc.Origin.WORKBENCH),
            parent_version_id=parent, operation_id=request.identity.operation_id,
            restored_from_version_id=request.source_version_id if request.operation_type == "restore" and reason == "postimage" else None,
            attempt_id=fence.attempt_id, generation=fence.generation)
        observation = self.ledger.append_observation(snapshot, context)
        if not self.ledger.verify_committed(observation):
            raise RuntimeError("Evidence was not committed")
        return observation
