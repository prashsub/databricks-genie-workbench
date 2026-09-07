"""Serialized full-state observation, independent of policy head authority."""

from dataclasses import replace
from datetime import datetime, timezone

from backend.services.version_control import contracts as vc


class Observer:
    def __init__(self, *, coordination, ledger, canonicalizer, transport, identity,
                 reader_selection, status_reader):
        self.coordination = coordination
        self.ledger = ledger
        self.canonicalizer = canonicalizer
        self.transport = transport
        self.identity = identity
        self.reader_selection = reader_selection
        self.status_reader = status_reader

    def capture_on_open(self, binding, viewer):
        status = self.status_reader(binding, viewer)
        executor = self.identity.executor(self.reader_selection)
        return self._capture(binding, "open", executor, status)

    def capture(self, binding, reason, executor):
        actor = vc.ActorContext(executor.principal_id, executor.workspace_id, executor.actor_kind)
        return self._capture(binding, reason, executor, self.status_reader(binding, actor))

    def _capture(self, binding, reason, executor, status):
        try:
            lease = self.coordination.observe_exclusively(binding, executor)
        except Exception:
            return vc.ObservationResult(replace(status, stale=True, allowed_actions=(),
                reasons=(*status.reasons, "Observation busy or unavailable")), None, True)
        actor = vc.ActorContext(executor.principal_id, executor.workspace_id, executor.actor_kind)
        status = self.status_reader(binding, actor)
        snapshot = self.canonicalizer.observe(self.transport.get(binding, executor))
        if status.heads.observed is not None:
            previous = self.ledger.get_version(binding, status.heads.observed)
            if self.canonicalizer.compare(previous.snapshot, snapshot) == vc.Comparison.EQUAL:
                self.coordination.advance_heads(lease.fence, vc.HeadUpdate(None, None, None, None))
                return vc.ObservationResult(replace(status, stale=False), None, False)
        context = vc.CaptureContext(binding, f"{lease.fence.attempt_id}:observe",
            datetime.now(timezone.utc), reason,
            vc.ActorContext(executor.principal_id, executor.workspace_id, executor.actor_kind),
            vc.Origin.EXTERNAL, parent_version_id=status.heads.observed,
            attempt_id=lease.fence.attempt_id, generation=lease.fence.generation)
        observation = self.ledger.append_observation(snapshot, context)
        if not self.ledger.verify_committed(observation):
            raise RuntimeError("Observation persistence unavailable")
        heads = self.coordination.advance_heads(lease.fence, vc.HeadUpdate(observation.version_id, None, None, None))
        summary = vc.VersionSummary(observation.version_id, binding.binding_id, context.observed_at,
            context.origin, context.actor.subject_id, context.parent_version_id, None,
            snapshot.fingerprints, None, None)
        return vc.ObservationResult(replace(status, heads=heads, observed_at=context.observed_at,
            projection_as_of=context.observed_at, drift=vc.DriftState.UNKNOWN,
            stale=False, allowed_actions=()), summary, False)
