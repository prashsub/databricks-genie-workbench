"""VC/1.0 orchestration; dependencies are injected domain ports."""

from dataclasses import dataclass

from backend.services.version_control import contracts as vc


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
                 executor: vc.ExecutorContext | None = None):
        self.canonicalizer = canonicalizer
        self.observer = observer
        self.executor = executor

    def prepare_choice(self, binding: vc.BindingRef) -> vc.ObservationResult:
        if self.observer is None or self.executor is None or self.executor.workspace_id != binding.workspace_id:
            raise RuntimeError("Target-local observer unavailable")
        captured = self.observer.capture(binding, "reconcile", self.executor)
        status = captured.status
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
