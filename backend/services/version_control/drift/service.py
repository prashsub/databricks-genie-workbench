"""VC/1.0 orchestration; dependencies are injected domain ports."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control import contracts as vc

from .ports import ReconcilePolicy


@dataclass(frozen=True)
class Classification(vc.DriftResult):
    common_base: str | None = None
    policy_target: str | None = None
    allowed_actions: tuple[str, ...] = ()
    quarantined: bool = False
    unresolved_operation_id: str | None = None
    conflicted: bool = False


class DriftService:
    def __init__(self, *, canonicalizer: vc.Canonicalizer, observer: vc.Observer | None = None,
                 executor: vc.ExecutorContext | None = None, ledger: vc.VersionLedger | None = None,
                 policy: ReconcilePolicy | None = None, approvals: vc.ApprovalService | None = None,
                 identity: vc.IdentityProvider | None = None, reconcile_enabled=False,
                 clock=None, dispatcher: vc.JobDispatcher | None = None,
                 facts: vc.OperationFacts | None = None, dispatch_enabled=False):
        self.canonicalizer = canonicalizer
        self.observer = observer
        self.executor = executor
        self.ledger = ledger
        self.policy = policy
        self.approvals = approvals
        self.identity = identity
        self.reconcile_enabled = reconcile_enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.dispatcher = dispatcher
        self.facts = facts
        self.dispatch_enabled = dispatch_enabled

    def reconcile(self, binding: vc.BindingRef, action: vc.ReconcileRequest,
                  actor: vc.ActorContext) -> vc.OperationHandle:
        if self.reconcile_enabled is not True:
            raise PermissionError("VC reconciliation writes disabled")
        if (self.identity is None or actor.workspace_id != binding.workspace_id
                or self.identity.can_edit(actor.subject_id, binding) is not True):
            raise PermissionError("Reconciliation requires target edit permission")
        if action.binding_revision != binding.binding_revision:
            raise ValueError("Binding revision changed")
        if action.action not in ("adopt", "reapply", "acknowledge"):
            raise ValueError("Unsupported reconciliation action")
        if action.action == "reapply" and (self.dispatch_enabled is not True or self.dispatcher is None):
            raise PermissionError("VC reconciliation Job dispatch disabled")
        if self.ledger is None or self.policy is None or self.approvals is None:
            raise RuntimeError("Reconciliation policy/evidence unavailable")
        captured = self.prepare_choice(binding)
        base = self.ledger.get_version(binding, captured.status.heads.observed)
        if (base.context.binding != binding or base.version_id != captured.status.heads.observed
                or base.snapshot.state_digest != action.expected_base):
            raise ValueError("Captured base changed; review the preserved observation")
        source = base
        if action.action in ("reapply", "acknowledge"):
            source = self.ledger.get_version(binding, captured.status.heads.approved)
            if source.context.binding != binding or source.version_id != captured.status.heads.approved:
                raise PermissionError("Policy target is not target-local")
        inputs = self.policy.inputs(binding, replace(action, approval_id=None), source, base, actor)
        if (inputs.target_binding != binding or inputs.operation_id != action.identity.operation_id
                or inputs.operation_type != action.action or inputs.requester_id != actor.subject_id
                or inputs.source_version_id != source.version_id
                or inputs.raw_source_digest != source.snapshot.raw_state_digest
                or inputs.source_fingerprints != source.snapshot.fingerprints
                or inputs.expected_base_fingerprints != base.snapshot.fingerprints
                or inputs.rendered_target_digest != source.snapshot.state_digest
                or inputs.canonicalizer_version != source.snapshot.fingerprints.canonicalizer_version
                or inputs.expires_at <= self.clock()):
            raise PermissionError("Policy inputs do not bind the captured target and source")
        if action.action == "acknowledge":
            reason = action.policy_inputs.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("Acknowledgement requires an explicit reason")
            inputs = replace(inputs, recovery_policy={**inputs.recovery_policy,
                             "acknowledged_heads": vc.to_wire(captured.status.heads), "reason": reason})
            if self.policy.authorize_acknowledgement(inputs, actor) is not True:
                raise PermissionError("Acknowledgement denied by policy")
            if self.facts is None:
                raise RuntimeError("Acknowledgement facts unavailable")
            self.facts.append(self._fact(binding, action, actor, inputs, base,
                                         vc.FactKind.ACKNOWLEDGEMENT, vc.FactStatus.ACKNOWLEDGED))
            return vc.OperationHandle(action.identity.operation_id, vc.OperationStatus.CONFIRMED, None)
        if action.action == "reapply" and action.approval_id:
            if self.facts is None:
                raise RuntimeError("Approval invalidation evidence unavailable")
            usage = self.facts.approval_use(binding, action.approval_id)
            if usage.ambiguous or usage.binding != binding or usage.approval_id != action.approval_id:
                raise PermissionError("Stale approval scope is ambiguous")
            self.facts.append(self._fact(binding, action, actor, inputs, base,
                                         vc.FactKind.APPROVAL_INVALIDATED, vc.FactStatus.INVALIDATED))
        self.approvals.request(inputs, actor)
        if action.action == "reapply":
            return self.dispatcher.submit_local(action.identity.operation_id, "reconcile")
        return vc.OperationHandle(action.identity.operation_id, vc.OperationStatus.REQUESTED, None)

    def with_acknowledgement(self, status: vc.BindingStatus, fact: vc.OperationFact) -> vc.BindingStatus:
        evidence = fact.evidence
        if (fact.fact_kind != vc.FactKind.ACKNOWLEDGEMENT or fact.status != vc.FactStatus.ACKNOWLEDGED
                or fact.binding.binding_id != status.binding_id
                or fact.binding.binding_revision != status.binding_revision
                or not isinstance(evidence, vc.ApprovalInputs) or evidence.expires_at <= self.clock()
                or evidence.recovery_policy.get("acknowledged_heads") != vc.to_wire(status.heads)):
            return status
        return replace(status, reasons=(*status.reasons, "Acknowledged divergence; policy heads remain unchanged"),
                       allowed_actions=tuple(action for action in status.allowed_actions if action != "acknowledge"))

    def _fact(self, binding, action, actor, inputs, base, kind, status):
        key = f"{action.identity.operation_id}:{kind.value}"
        return vc.OperationFact(str(uuid5(NAMESPACE_URL, key)), kind, action.identity.operation_id,
                                0, key, binding, action.identity, action.action, actor.subject_id,
                                actor, status, inputs, self.clock(),
                                expected_base_fingerprint=base.snapshot.state_digest,
                                pre_version_id=base.version_id, approval_id=action.approval_id)

    def prepare_choice(self, binding: vc.BindingRef) -> vc.ObservationResult:
        if self.observer is None or self.executor is None or self.executor.workspace_id != binding.workspace_id:
            raise RuntimeError("Target-local observer unavailable")
        captured = self.observer.capture(binding, "reconcile", self.executor)
        status = captured.status
        if status.drift == vc.DriftState.UNKNOWN and not captured.busy and not status.stale and self.ledger is not None:
            class BoundVersions:
                def get(inner, reference):
                    version = self.ledger.get_version(binding, reference)
                    if version.context.binding != binding or version.version_id != reference:
                        raise PermissionError("History binding mismatch")
                    return version.snapshot

            result = self.classify(status.heads, BoundVersions(), "reachable",
                                   quarantined=status.quarantined,
                                   unresolved_operation_id=status.unresolved_operation_id)
            status = replace(status, drift=result.state, reasons=result.reasons,
                             allowed_actions=result.allowed_actions)
            captured = replace(captured, status=status)
        if (captured.busy or status.stale or status.quarantined or status.unresolved_operation_id
                or status.binding_id != binding.binding_id
                or status.binding_revision != binding.binding_revision
                or status.heads.observed is None
                or status.drift in (vc.DriftState.UNKNOWN, vc.DriftState.UNREACHABLE,
                                    vc.DriftState.APPLIED_UNVERIFIED, vc.DriftState.CONFLICTED)):
            raise RuntimeError("Fresh durable observation unavailable or binding blocked")
        return captured

    def classify(self, heads: vc.Heads, versions: vc.VersionLookup, reachability: str, *,
                 unresolved_operation_id=None, quarantined=False, conflicted=False):
        reasons = []
        state = None
        if quarantined:
            state = vc.DriftState.UNKNOWN
            reasons.append("Binding quarantined; manual identity/recovery resolution required")
        if conflicted:
            state = vc.DriftState.CONFLICTED
            reasons.append("Conflicting policy authority")
        if unresolved_operation_id:
            state = vc.DriftState.APPLIED_UNVERIFIED
            reasons.append("Unresolved operation; application not verified")
        if reachability != "reachable":
            state = vc.DriftState.UNREACHABLE if reachability == "unreachable" else vc.DriftState.UNKNOWN
            reasons.append("Live state unreachable" if reachability == "unreachable" else "Reachability unknown")
        if state is not None:
            return Classification(state, tuple(reasons), quarantined=quarantined,
                                  unresolved_operation_id=unresolved_operation_id, conflicted=conflicted)
        if None in (heads.observed, heads.approved, heads.deployed):
            return Classification(vc.DriftState.UNKNOWN, ("Missing policy or observation head",))
        try:
            observed, approved, deployed = (versions.get(reference) for reference in
                                            (heads.observed, heads.approved, heads.deployed))
        except (LookupError, PermissionError):
            return Classification(vc.DriftState.UNKNOWN, ("Required history unavailable",))
        observed_approved = self.canonicalizer.compare(observed, approved)
        approved_deployed = self.canonicalizer.compare(approved, deployed)
        observed_deployed = self.canonicalizer.compare(observed, deployed)
        if vc.Comparison.UNKNOWN in (observed_approved, approved_deployed, observed_deployed):
            return Classification(vc.DriftState.UNKNOWN, ("Incompatible canonicalizer evidence",))
        if observed_approved == approved_deployed == vc.Comparison.EQUAL:
            state = vc.DriftState.CLEAN
        elif approved_deployed == vc.Comparison.EQUAL:
            state = vc.DriftState.EXTERNAL_AHEAD
        elif vc.Comparison.EQUAL in (observed_deployed, observed_approved):
            state = vc.DriftState.DESIRED_AHEAD
        else:
            state = vc.DriftState.DIVERGED
        common = heads.deployed if heads.deployed in (heads.observed, heads.approved) else None
        return Classification(state, ("Compared observed against approved and deployed policy",),
                              common, heads.approved, ("compare", "adopt", "reapply", "acknowledge"))
