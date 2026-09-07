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
    governed_by: str = "workbench"


@dataclass(frozen=True)
class BundleResource(vc.WireValue):
    workspace_id: str
    space_id: str | None
    bundle: str
    resource_path: str
    manages_content: bool
    audit_actor: str | None


@dataclass(frozen=True)
class BundleSnapshot(vc.WireValue):
    resources: tuple[BundleResource, ...]
    complete: bool


class BundleInventory(Protocol):
    def for_binding(self, binding: vc.BindingRef) -> BundleSnapshot: ...


class BindingInventory(Protocol):
    def page(self, workspace_id: str, cursor: str | None, limit: int) -> vc.Page[ScanEntry]: ...

    def matches(self, binding: vc.BindingRef) -> tuple[vc.BindingRef, ...]:
        """All physical/logical identity matches across pages, including duplicate rows."""
        ...


class Projections(Protocol):
    def publish(self, binding: vc.BindingRef, status: vc.BindingStatus,
                full_fetch_at: datetime | None, api_update_time: datetime | None) -> None: ...
