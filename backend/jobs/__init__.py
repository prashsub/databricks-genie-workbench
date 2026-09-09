"""Composition boundary for module-owned native Job handlers.

`build_vc_runtime(kind)` is the single target-local composition point invoked by
the `vc-platform` wheel entry point (`platform.jobs:main`) for every governed job
kind. It resolves the durable platform ports for the target workspace and wires
them into a `GovernedJobRuntime`, returning a `VcJobRuntime` facade that exposes
both `.run(kind, operation_id)` (the dispatch entry) and the individual ports the
module job entrypoints read directly (e.g. `backend/jobs/vc_reconcile.py`).

Fail-closed contract: the deep per-port construction (Delta-backed operation
facts, the mutation-gate graph, identity verification, explicit-credential SDK
clients) lives behind `_resolve_platform_ports`, which requires a fully
provisioned, explicitly configured target workspace. Until that lands and is
platform-validated it raises "not integrated", so `main()` and every job
entrypoint remain disabled by default. The wiring graph itself
(`assemble_vc_runtime`) is injectable and offline-tested with fakes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.services.version_control.platform.capabilities import (
    FIRST_WRITE_CAPABILITIES,
    capabilities_ready,
)
from backend.services.version_control.platform.jobs import JOB_KINDS, GovernedJobRuntime
from backend.services.version_control.platform.provisioning import provision

# Per-kind write switch consulted (in addition to vc_writes_enabled + live
# capabilities) before a governed mutation handler is allowed to run.
_KIND_SWITCH = {
    "restore": "vc_restore_enabled",
    "promotion": "vc_promotion_enabled",
    "optimizer_apply": "vc_optimizer_apply_enabled",
    "reconcile": "vc_reconcile_enabled",
}


@dataclass(frozen=True)
class VcJobRuntime:
    """Facade returned by `build_vc_runtime`: a `.run()` dispatcher plus the
    individual ports read directly by module job entrypoints."""

    workspace_id: str
    facts: Any = None
    gate: Any = None
    identity: Any = None
    executor: Any = None
    flags: Any = None
    service: Any = None
    _governed: GovernedJobRuntime | None = None
    _provision: Callable[[str], Any] | None = None

    def run(self, kind: str, operation_id: str):
        if kind == "provision":
            if self._provision is None:
                raise PermissionError(
                    "VC provision runtime not integrated; provision and validate "
                    "the target workspace first")
            return self._provision(operation_id)
        if self._governed is None:
            raise PermissionError(f"VC {kind} runtime not integrated")
        return self._governed.run(kind, operation_id)


def _make_write_ready(flags, capability_probe) -> Callable[[str], bool]:
    """A write is ready only when vc_writes_enabled, the per-kind switch (where
    one exists) and every first-write capability are simultaneously satisfied.
    A capability outage never reuses an earlier positive result."""

    def ready(kind: str) -> bool:
        if flags.enabled("vc_writes_enabled") is not True:
            return False
        switch = _KIND_SWITCH.get(kind)
        if switch is not None and flags.enabled(switch) is not True:
            return False
        return capabilities_ready(capability_probe, FIRST_WRITE_CAPABILITIES)

    return ready


def assemble_vc_runtime(*, workspace_id, facts, flags, executor, identity, gate,
                        verify_only, attempt_state, capability_probe,
                        restore_service=None, optimizer_adapter=None,
                        reconcile_service=None) -> VcJobRuntime:
    """Wire injected ports into a governed runtime.

    Only handlers verified end-to-end are wired; every other governed kind stays
    absent from the handler map and therefore fails closed through
    `GovernedJobRuntime` ("Job handler not integrated") until it is wired and
    platform-validated. `reconcile_service` is accepted and surfaced as
    `.service` for the direct-attribute reconcile entrypoint even though its
    `.run` dispatch handler is not wired yet.
    """

    handlers: dict[str, Callable[[Any], Any]] = {}
    if restore_service is not None:
        handlers["restore"] = lambda request: restore_service.run(
            request.identity.operation_id, executor)
    if optimizer_adapter is not None:
        handlers["optimizer_apply"] = lambda request: optimizer_adapter.apply(
            request.optimizer_run_id, request.champion_id, request.binding,
            request.expected_base, executor)

    governed = GovernedJobRuntime(facts, workspace_id, handlers, verify_only,
                                  attempt_state, _make_write_ready(flags, capability_probe))
    return VcJobRuntime(workspace_id=workspace_id, facts=facts, gate=gate,
                        identity=identity, executor=executor, flags=flags,
                        service=reconcile_service, _governed=governed)


def assemble_provision_runtime(*, workspace_id, manifests, runner) -> VcJobRuntime:
    """Bootstrap provisioning runtime.

    `provision` is the one governed kind that cannot flow through
    `GovernedJobRuntime`: it precedes the durable fact tables it would otherwise
    load a request from (chicken-and-egg). It instead runs the reviewed owner
    migrations and grants directly, guarded by `provision()`'s manifest checks
    and the runner's per-spec validation. No Genie content is ever touched.
    """

    def _run_provision(operation_id: str):
        # operation_id is validated canonical by `main()`; provisioning is
        # infrastructure bootstrap and consumes no durable request.
        provision(manifests, runner)

    return VcJobRuntime(workspace_id=workspace_id, _provision=_run_provision)


def _resolve_platform_ports(kind: str) -> dict:
    """Construct durable target-local ports for `kind` from explicit workspace
    configuration and credentials.

    Platform seam: this builds the Delta-backed operation facts, the mutation
    gate graph (coordination/ledger/approvals/transport/canonicalizer), the
    verified identity provider and explicit-credential SDK clients from the
    provisioned target workspace, then returns the keyword bundle for
    `assemble_vc_runtime`. It requires a fully provisioned, explicitly selected
    non-production target (Delta tables, run-as service principal, warehouse and
    deployed job ids) and is validated during the provisioning/integration
    phase. Until then it fails closed.
    """

    raise PermissionError(
        f"VC {kind} runtime not integrated; target-local platform ports are "
        "unavailable (provision and validate the target workspace first)")


def build_vc_runtime(kind: str) -> VcJobRuntime:
    if kind not in JOB_KINDS:
        raise PermissionError(f"VC {kind} runtime not integrated; unknown job kind")
    ports = _resolve_platform_ports(kind)
    if kind == "provision":
        return assemble_provision_runtime(**ports)
    return assemble_vc_runtime(**ports)
