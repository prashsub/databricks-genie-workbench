"""VC/1.0 orchestration; dependencies are injected domain ports."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control import contracts as vc

from .errors import ReconcileError
from .ports import ApprovedTargets, BindingInventory, BundleInventory, CoordinationReadiness, Projections, ReconcilePolicy


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
                 facts: vc.OperationFacts | None = None, dispatch_enabled=False,
                 inventory: BindingInventory | None = None, projections: Projections | None = None,
                 scan_enabled=False, batch_size=100, full_fetch_interval=timedelta(minutes=30),
                 bundles: BundleInventory | None = None, coordination: vc.Coordination | None = None,
                 registry: vc.Registry | None = None, authorize_history=None,
                 stale_after=timedelta(minutes=30), approved_targets: ApprovedTargets | None = None,
                 readiness: CoordinationReadiness | None = None):
        if not 1 <= batch_size <= 100 or full_fetch_interval <= timedelta(0):
            raise ValueError("Invalid scan bounds or full-fetch interval")
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
        self.inventory = inventory
        self.projections = projections
        self.scan_enabled = scan_enabled
        self.batch_size = batch_size
        self.full_fetch_interval = full_fetch_interval
        self.bundles = bundles
        self.coordination = coordination
        self.registry = registry
        self.authorize_history = authorize_history
        self.stale_after = stale_after
        self.approved_targets = approved_targets
        self.readiness = readiness

    def overview(self, actor: vc.ActorContext, cursor: str | None = None, limit: int = 100) -> vc.OverviewPage:
        if not 1 <= limit <= 100:
            raise ValueError("Overview limit must be between 1 and 100")
        if self.projections is None or self.registry is None:
            raise RuntimeError("Projection reader unavailable")
        page = self.projections.page(actor, cursor, limit)
        if len(page.items) > limit:
            raise ValueError("Projection page exceeded requested bound")
        items = []
        for status in page.items:
            binding = self.registry.resolve(status.binding_id)
            self._history_scope(binding, actor)
            items.append(self._projected_status(binding, status))
        return vc.OverviewPage(tuple(items), page.next_cursor)

    def status(self, binding: vc.BindingRef, actor: vc.ActorContext) -> vc.BindingStatus:
        self._history_scope(binding, actor)
        if self.projections is None:
            raise RuntimeError("Projection reader unavailable")
        return self._projected_status(binding, self.projections.get(binding, actor))

    def history(self, binding: vc.BindingRef, actor: vc.ActorContext,
                cursor: str | None = None, limit: int = 100) -> vc.VersionPage:
        self._history_scope(binding, actor)
        if not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        if self.ledger is None:
            raise RuntimeError("History reader unavailable")
        page = self.ledger.history(binding, cursor, limit)
        if len(page.items) > limit or any(item.binding_id != binding.binding_id for item in page.items):
            raise PermissionError("History page scope or bound mismatch")
        return page

    def _history_scope(self, binding, actor):
        if (binding.workspace_id != actor.workspace_id or self.authorize_history is None
                or self.authorize_history(actor, binding) is not True):
            raise PermissionError("Binding history scope denied")

    def _projected_status(self, binding, status):
        if status.binding_id != binding.binding_id or status.binding_revision != binding.binding_revision:
            raise PermissionError("Projection binding scope mismatch")
        if self.approved_targets is None:
            status = replace(status, drift=vc.DriftState.UNKNOWN, stale=True, allowed_actions=(),
                             reasons=(*status.reasons, "Target-local rendered approval evidence unavailable or invalid"))
        try:
            self._require_coordination(binding)
        except Exception:
            status = replace(status, stale=True, allowed_actions=(),
                             reasons=(*status.reasons, "Coordination unavailable; history remains read-only"))
        stale = (status.stale or status.projection_as_of > self.clock()
                 or self.clock() - status.projection_as_of >= self.stale_after
                 or status.observed_at is None or status.observed_at > self.clock()
                 or self.clock() - status.observed_at >= self.stale_after)
        blocked = stale or status.quarantined or status.unresolved_operation_id or status.drift in (
            vc.DriftState.UNKNOWN, vc.DriftState.UNREACHABLE, vc.DriftState.APPLIED_UNVERIFIED, vc.DriftState.CONFLICTED)
        actions = () if blocked else tuple(action for action in status.allowed_actions
            if action == "compare" or (self.reconcile_enabled is True
                and action in ("adopt", "acknowledge", "reapply")
                and (action != "reapply" or self.dispatch_enabled is True)))
        return replace(status, stale=stale, allowed_actions=actions,
                       reasons=(*status.reasons, "Projection stale; refresh via observer") if stale else status.reasons)

    def scan(self, workspace_id: str, cursor: str | None) -> vc.ReconcileBatch:
        if self.scan_enabled is not True:
            raise PermissionError("VC scheduled observation disabled")
        if self.executor is None or self.executor.workspace_id != workspace_id:
            raise PermissionError("Scan executor workspace mismatch")
        if self.inventory is None or self.projections is None or self.observer is None:
            raise RuntimeError("Scan inventory, projection or observer unavailable")
        page = self.inventory.page(workspace_id, cursor, self.batch_size)
        if len(page.items) > self.batch_size:
            raise ValueError("Inventory exceeded scan bound")
        results = []
        for entry in page.items:
            status = entry.status
            full_fetch_at = entry.last_full_fetch_at
            try:
                if (entry.binding.workspace_id != workspace_id
                        or status.binding_id != entry.binding.binding_id
                        or status.binding_revision != entry.binding.binding_revision):
                    raise PermissionError("Inventory binding scope mismatch")
                if self.ledger is None:
                    status = self._observed_status(entry.binding, status)
                    raise RuntimeError("Ledger unavailable; cannot verify observed head")
                if self.approved_targets is None:
                    status = replace(status, drift=vc.DriftState.UNKNOWN, allowed_actions=(),
                                     reasons=(*status.reasons, "Target-local rendered approval evidence unavailable or invalid"))
                    raise RuntimeError("Approved targets unavailable; cannot verify rendered approval")
                matches = self.inventory.matches(entry.binding)
                if len(matches) != 1 or matches[0] != entry.binding:
                    reason = "Ambiguous binding inventory; audited manual identity resolution required"
                    status = replace(status, drift=vc.DriftState.UNKNOWN, quarantined=True,
                                     allowed_actions=(), reasons=(*status.reasons, reason))
                    if self.coordination is None:
                        raise RuntimeError("Identity quarantine coordinator unavailable")
                    lease = self.coordination.observe_exclusively(entry.binding, self.executor)
                    if (lease.fence.binding_id != entry.binding.binding_id
                            or lease.fence.binding_revision != entry.binding.binding_revision):
                        raise PermissionError("Identity quarantine fence mismatch")
                    self.coordination.quarantine(lease.fence, reason)
                    self.projections.publish(entry.binding, status, entry.last_full_fetch_at,
                                             entry.previous_update_time)
                    results.append(status)
                    continue
                conflicts = ()
                if entry.governed_by == "workbench":
                    status = replace(status, drift=vc.DriftState.UNKNOWN, allowed_actions=())
                    conflicts = self._bundle_conflicts(entry.binding)
                    status = self._bundle_status(entry.status, conflicts)
                due = (conflicts or status.stale or full_fetch_at is None or full_fetch_at > self.clock()
                       or self.clock() - full_fetch_at >= self.full_fetch_interval
                       or entry.api_update_time is None or entry.api_update_time != entry.previous_update_time)
                if due:
                    captured = self.observer.capture(entry.binding, "scheduled_reconcile", self.executor)
                    status = captured.status
                    if (status.binding_id != entry.binding.binding_id
                            or status.binding_revision != entry.binding.binding_revision):
                        status = entry.status
                        raise PermissionError("Observation binding scope mismatch")
                    if captured.busy or status.stale:
                        status = self._bundle_status(status, conflicts)
                        raise RuntimeError("Observation busy or unavailable")
                    status = self._observed_status(entry.binding, status)
                    status = self._bundle_status(status, conflicts)
                    full_fetch_at = self.clock()
                self.projections.publish(entry.binding, status, full_fetch_at, entry.api_update_time)
            except Exception:
                status = replace(status, stale=True, allowed_actions=(),
                                 reasons=(*status.reasons, "Reconciliation evidence unavailable; no mutation replay"))
                if full_fetch_at == entry.last_full_fetch_at:
                    try:
                        self.projections.publish(entry.binding, status, entry.last_full_fetch_at,
                                                 entry.previous_update_time)
                    except Exception:
                        pass
            results.append(status)
        return vc.ReconcileBatch(tuple(results), page.next_cursor)

    def _bundle_conflicts(self, binding):
        if self.bundles is None:
            raise RuntimeError("Sanctioned bundle inventory unavailable")
        snapshot = self.bundles.for_binding(binding)
        if snapshot.complete is not True:
            raise RuntimeError("Sanctioned bundle inventory incomplete")
        return tuple(resource for resource in snapshot.resources
                     if resource.workspace_id == binding.workspace_id
                     and resource.space_id == binding.space_id
                     and resource.manages_content
                     and resource.resource_path.startswith("resources.genie_spaces."))

    def _require_single_authority(self, binding):
        conflicts = self._bundle_conflicts(binding)
        if conflicts:
            details = "; ".join(f"{resource.bundle} {resource.resource_path}" for resource in conflicts)
            raise ReconcileError("dual_authority", f"Dual authority: {details}", http_status=409)

    def _bundle_status(self, status, conflicts):
        if not conflicts:
            return status
        reasons = tuple(f"Dual authority: {resource.bundle} {resource.resource_path}; "
                        f"actor={resource.audit_actor or 'unknown'}" for resource in conflicts)
        return replace(status, drift=vc.DriftState.CONFLICTED, allowed_actions=(),
                       reasons=(*status.reasons, *reasons))

    def reconcile(self, binding: vc.BindingRef, action: vc.ReconcileRequest,
                  actor: vc.ActorContext) -> vc.OperationHandle:
        try:
            return self._reconcile(binding, action, actor)
        except ReconcileError as error:
            if error.error.operation_id is None:
                error.error = replace(error.error, operation_id=action.identity.operation_id)
            raise
        except (ValueError, TypeError) as error:
            raise ReconcileError("request_conflict", str(error), http_status=409,
                                 operation_id=action.identity.operation_id) from error
        except PermissionError:
            raise
        except Exception as error:
            raise ReconcileError("evidence_unavailable", "Coordination or evidence unavailable; poll operation, do not replay",
                                 operation_id=action.identity.operation_id) from error

    def _require_coordination(self, binding):
        if self.readiness is None:
            raise RuntimeError("Authoritative coordination readiness unavailable")
        self.readiness.assert_available(binding)

    def _reconcile(self, binding, action, actor):
        if self.reconcile_enabled is not True:
            raise PermissionError("VC reconciliation writes disabled")
        if (self.identity is None or actor.workspace_id != binding.workspace_id
                or self.identity.can_edit(actor.subject_id, binding) is not True):
            raise PermissionError("Reconciliation requires target edit permission")
        if action.binding_revision != binding.binding_revision:
            raise ValueError("Binding revision changed")
        if {"automatic_merge", "merged_payload", "serialized_space", "sql"} & action.policy_inputs.keys():
            raise ValueError("Manual merge requires a reviewed new payload through the gated edit flow")
        if action.action not in ("adopt", "reapply", "acknowledge"):
            raise ValueError("Unsupported reconciliation action")
        if action.action == "reapply" and (self.dispatch_enabled is not True or self.dispatcher is None):
            raise PermissionError("VC reconciliation Job dispatch disabled")
        if self.ledger is None or self.policy is None or self.approvals is None:
            raise RuntimeError("Reconciliation policy/evidence unavailable")
        self._require_coordination(binding)
        self._require_single_authority(binding)
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

    def compare(self, binding: vc.BindingRef, left: str, right: str,
                actor: vc.ActorContext) -> vc.SemanticDiff:
        if (self.identity is None or actor.workspace_id != binding.workspace_id
                or self.identity.can_edit(actor.subject_id, binding) is not True):
            raise PermissionError("Comparison scope denied")
        if self.ledger is None:
            raise RuntimeError("History unavailable")
        versions = [self.ledger.get_version(binding, reference) for reference in (left, right)]
        if any(version.context.binding != binding or version.version_id != reference
               for version, reference in zip(versions, (left, right))):
            raise PermissionError("Comparison history binding mismatch")
        before, after = (version.snapshot for version in versions)
        comparison = self.canonicalizer.compare(before, after)
        if comparison == vc.Comparison.UNKNOWN:
            return vc.SemanticDiff(comparison, ())
        items = self.canonicalizer.semantic_diff(before, after)
        return vc.SemanticDiff(comparison, tuple(replace(item, review_required=True)
                                                if item.category == vc.DiffCategory.SQL else item for item in items))

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
        if (status.quarantined or status.unresolved_operation_id
                or status.drift == vc.DriftState.APPLIED_UNVERIFIED):
            raise ReconcileError("binding_locked", "Quarantine or unresolved operation requires authorized recovery", http_status=423)
        if not captured.busy and not status.stale:
            status = self._observed_status(binding, status)
            captured = replace(captured, status=status)
        if (captured.busy or status.stale or status.quarantined or status.unresolved_operation_id
                or status.binding_id != binding.binding_id
                or status.binding_revision != binding.binding_revision
                or status.heads.observed is None
                or status.drift in (vc.DriftState.UNKNOWN, vc.DriftState.UNREACHABLE,
                                    vc.DriftState.APPLIED_UNVERIFIED, vc.DriftState.CONFLICTED)):
            raise RuntimeError("Fresh durable observation unavailable or binding blocked")
        return captured

    def _observed_status(self, binding, status):
        if self.ledger is None:
            return replace(status, drift=vc.DriftState.UNKNOWN, stale=True, allowed_actions=(),
                           reasons=(*status.reasons, "Ledger unavailable; cannot verify observed head"))
        if status.drift in (vc.DriftState.UNREACHABLE, vc.DriftState.CONFLICTED,
                            vc.DriftState.APPLIED_UNVERIFIED):
            return status

        class BoundVersions:
            def get(inner, reference):
                version = self.ledger.get_version(binding, reference)
                if version.context.binding != binding or version.version_id != reference:
                    raise PermissionError("History binding mismatch")
                return version.snapshot

        result = self.classify(status.heads, BoundVersions(), "reachable", quarantined=status.quarantined,
                               unresolved_operation_id=status.unresolved_operation_id, target_binding=binding)
        return replace(status, drift=result.state, reasons=result.reasons, allowed_actions=result.allowed_actions)

    def classify(self, heads: vc.Heads, versions: vc.VersionLookup, reachability: str, *,
                 unresolved_operation_id=None, quarantined=False, conflicted=False, target_binding=None):
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
        if self.approved_targets is None:
            return Classification(vc.DriftState.UNKNOWN, ("Target-local rendered approval evidence unavailable or invalid",))
        else:
            try:
                if target_binding is None:
                    raise ValueError("Target binding required for rendered approval")
                target = self.approved_targets.resolve(target_binding, heads.approved)
                inputs = target.approval.request.inputs
                preflight = target.preflight
                if (target.head_id != heads.approved or target.approval.status != vc.FactStatus.APPROVED
                        or target.approval.approval_digest is None or inputs.target_binding != target_binding
                        or inputs.source_fingerprints != approved.fingerprints
                        or inputs.raw_source_digest != approved.raw_state_digest
                        or inputs.rendered_target_digest != target.rendered.state_digest
                        or target.rendered.state_digest != target.rendered.fingerprints.state_digest
                        or inputs.canonicalizer_version != target.rendered.fingerprints.canonicalizer_version
                        or preflight.target_binding != target_binding or preflight.operation_id != inputs.operation_id
                        or preflight.rendered_fingerprints != target.rendered.fingerprints
                        or preflight.expected_base_fingerprints != inputs.expected_base_fingerprints
                        or preflight.preflight_evidence_digest != inputs.preflight_evidence_digest):
                    raise ValueError("Rendered target approval evidence mismatch")
                approved = target.rendered
            except (LookupError, PermissionError, ValueError, RuntimeError):
                return Classification(vc.DriftState.UNKNOWN, ("Target-local rendered approval evidence unavailable or invalid",))
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
