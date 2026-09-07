"""VC/1.0 orchestration; dependencies are injected domain ports."""

from dataclasses import dataclass

from backend.services.version_control import contracts as vc


@dataclass(frozen=True)
class Classification(vc.DriftResult):
    common_base: str | None = None
    policy_target: str | None = None
    allowed_actions: tuple[str, ...] = ()


class DriftService:
    def __init__(self, *, canonicalizer: vc.Canonicalizer):
        self.canonicalizer = canonicalizer

    def classify(self, heads: vc.Heads, versions: vc.VersionLookup, reachability: str):
        observed, approved, deployed = (versions.get(reference) for reference in
                                        (heads.observed, heads.approved, heads.deployed))
        observed_approved = self.canonicalizer.compare(observed, approved)
        approved_deployed = self.canonicalizer.compare(approved, deployed)
        observed_deployed = self.canonicalizer.compare(observed, deployed)
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
