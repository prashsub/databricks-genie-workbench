"""M05 composition seams using VC/1.0 values; no local policy authority."""

from typing import Protocol
from dataclasses import dataclass
from datetime import datetime

from backend.services.version_control import contracts as vc


class ReconcilePolicy(Protocol):
    def inputs(self, binding: vc.BindingRef, request: vc.ReconcileRequest,
               source: vc.Version, base: vc.Version, actor: vc.ActorContext) -> vc.ApprovalInputs: ...

    def authorize_acknowledgement(self, inputs: vc.ApprovalInputs, actor: vc.ActorContext) -> bool: ...


@dataclass(frozen=True)
class ScanEntry(vc.WireValue):
    binding: vc.BindingRef
    status: vc.BindingStatus
    api_update_time: datetime | None
    previous_update_time: datetime | None
    last_full_fetch_at: datetime | None


class BindingInventory(Protocol):
    def page(self, workspace_id: str, cursor: str | None, limit: int) -> vc.Page[ScanEntry]: ...


class Projections(Protocol):
    def publish(self, binding: vc.BindingRef, status: vc.BindingStatus,
                full_fetch_at: datetime | None, api_update_time: datetime | None) -> None: ...
