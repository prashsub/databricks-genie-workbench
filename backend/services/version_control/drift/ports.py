"""M05 composition seams using VC/1.0 values; no local policy authority."""

from typing import Protocol

from backend.services.version_control import contracts as vc


class ReconcilePolicy(Protocol):
    def inputs(self, binding: vc.BindingRef, request: vc.ReconcileRequest,
               source: vc.Version, base: vc.Version, actor: vc.ActorContext) -> vc.ApprovalInputs: ...

    def authorize_acknowledgement(self, inputs: vc.ApprovalInputs, actor: vc.ActorContext) -> bool: ...
