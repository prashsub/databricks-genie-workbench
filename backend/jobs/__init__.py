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

from backend.services.version_control.platform.attempt import build_attempt_state
from backend.services.version_control.platform.capabilities import (
    FIRST_WRITE_CAPABILITIES,
    capabilities_ready,
)
from backend.services.version_control.platform.feature_flags import FeatureFlags
from backend.services.version_control.platform.jobs import JOB_KINDS, GovernedJobRuntime
from backend.services.version_control.platform.provisioning import provision

# Per-kind write switch consulted (in addition to vc_writes_enabled + live
# capabilities) before a governed mutation handler is allowed to run.
_KIND_SWITCH = {
    "restore": "vc_restore_enabled",
    "promotion": "vc_promotion_enabled",
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
                        restore_service=None,
                        promotion_service=None, reconcile_service=None) -> VcJobRuntime:
    """Wire injected ports into a governed runtime.

    Only handlers verified end-to-end are wired; every other governed kind stays
    absent from the handler map and therefore fails closed through
    `GovernedJobRuntime` ("Job handler not integrated") until it is wired and
    platform-validated. `reconcile_service` is accepted and surfaced as
    `.service` for the direct-attribute reconcile entrypoint even though its
    `.run` dispatch handler is not wired yet (reconcile reapply flows through the
    gate via `vc_reconcile.run_reapply`, not a `.run` handler).
    """

    handlers: dict[str, Callable[[Any], Any]] = {}
    if restore_service is not None:
        handlers["restore"] = lambda request: restore_service.run(
            request.identity.operation_id, executor)
    if promotion_service is not None:
        handlers["promotion"] = lambda request: promotion_service.execute(
            request.identity.operation_id, executor)

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


_PROVISION_CONFIG_KEYS = ("catalog", "control_schema", "warehouse_id", "principals")


def resolve_provision_ports(*, config, execute) -> dict:
    """Build the provisioning ports (manifests + owner runner) from explicit
    config and a blocking SQL `execute` seam.

    Pure and offline-testable: the reviewed owner DDL manifests and the
    least-privilege grant matrix are assembled here and handed to an
    `OwnerMigrationRunner` bound to `execute`. Fails closed if the explicit
    catalog/control-schema/warehouse/principals config is incomplete.
    """

    from backend.services.version_control.platform.provisioning import (
        OwnerMigrationRunner,
        build_grant_matrix,
        build_owner_manifests,
    )

    if not config or any(not config.get(key) for key in _PROVISION_CONFIG_KEYS):
        raise PermissionError(
            "VC provision runtime not integrated; explicit catalog, control "
            "schema, warehouse and principals are required")
    grants = build_grant_matrix(config["principals"])
    runner = OwnerMigrationRunner(execute, config["catalog"], config["control_schema"], grants)
    return {"workspace_id": config.get("workspace_id", ""),
            "manifests": build_owner_manifests(), "runner": runner}


_GOVERNED_CONFIG_KEYS = ("workspace_id", "catalog", "control_schema", "warehouse_id", "principals")


@dataclass(frozen=True)
class GovernedSeams:
    """Injected, credential/policy-heavy leaf constructors for the governed graph.

    `resolve_governed_ports` fixes the dependency *order*, the fail-closed config
    gating and the `verify_only`/`attempt_state` closures; the leaf ports that
    need live credentials (Delta SQL, volumes, SDK clients) or admission policy
    (the coordination service's authorization/enrollment/recovery callbacks) are
    built by these seams. Offline tests inject fakes; the real factories are
    supplied by `_resolve_platform_ports` and validated on the live platform.

    Each seam receives the resolved `config` plus already-built collaborators as
    keyword arguments, so the builder never reaches for ambient state.
    """

    facts: Callable[..., Any]
    ledger: Callable[..., Any]
    registry: Callable[..., Any]
    canonicalizer: Callable[..., Any]
    identity: Callable[..., Any]
    executor: Callable[..., Any]
    coordination: Callable[..., Any]
    approvals: Callable[..., Any]
    transport: Callable[..., Any]
    gate: Callable[..., Any]
    capability_probe: Callable[..., Any]
    dispatcher: Callable[..., Any] | None = None
    restore: Callable[..., Any] | None = None
    promotion: Callable[..., Any] | None = None
    reconcile: Callable[..., Any] | None = None


def resolve_governed_ports(*, config, seams: GovernedSeams) -> dict:
    """Pure builder for the governed mutation runtime kwargs.

    Assembles the target-local graph (facts -> ledger/registry -> coordination ->
    approvals/transport -> gate -> handlers) in strict dependency order from
    explicit `config` and the injected `seams`, then returns the exact kwargs
    `assemble_vc_runtime` consumes. `verify_only` is pinned to the constructed
    gate + executor and `attempt_state` to the durable facts so the job runtime
    and the gate can never disagree. Fails closed if the explicit
    workspace/catalog/control-schema/warehouse/principals config is incomplete;
    the restore/reconcile handlers are wired only when their seam is
    provided (mirroring `assemble_vc_runtime`'s optional handlers).
    """

    if not config or any(not config.get(key) for key in _GOVERNED_CONFIG_KEYS):
        raise PermissionError(
            "VC governed runtime not integrated; explicit workspace, catalog, "
            "control schema, warehouse and principals are required")

    flags = FeatureFlags.from_config(config.get("flags", {}))

    facts = seams.facts(config)
    ledger = seams.ledger(config)
    registry = seams.registry(config)
    canonicalizer = seams.canonicalizer(config)
    identity = seams.identity(config)
    executor = seams.executor(config, identity=identity)
    coordination = seams.coordination(
        config, facts=facts, ledger=ledger, registry=registry, canonicalizer=canonicalizer)
    approvals = seams.approvals(config, facts=facts, identity=identity, registry=registry)
    transport = seams.transport(
        config, executor=executor, coordination=coordination, registry=registry, flags=flags)
    gate = seams.gate(
        coordination=coordination, ledger=ledger, facts=facts, approvals=approvals,
        transport=transport, canonicalizer=canonicalizer, flags=flags, registry=registry)
    capability_probe = seams.capability_probe(config)

    dispatcher = seams.dispatcher(
        config, identity=identity, executor=executor, facts=facts) if seams.dispatcher else None

    ports: dict[str, Any] = {
        "workspace_id": config["workspace_id"],
        "facts": facts,
        "flags": flags,
        "executor": executor,
        "identity": identity,
        "gate": gate,
        "verify_only": (lambda operation_id: gate.verify_only(operation_id, executor)),
        "attempt_state": build_attempt_state(facts),
        "capability_probe": capability_probe,
    }
    if seams.restore is not None:
        ports["restore_service"] = seams.restore(
            facts=facts, ledger=ledger, gate=gate, dispatcher=dispatcher,
            identity=identity, flags=flags)
    if seams.promotion is not None:
        ports["promotion_service"] = seams.promotion(
            config, facts=facts, gate=gate, approvals=approvals, identity=identity,
            ledger=ledger, canonicalizer=canonicalizer, registry=registry,
            transport=transport, flags=flags, executor=executor, dispatcher=dispatcher)
    if seams.reconcile is not None:
        ports["reconcile_service"] = seams.reconcile(
            config, canonicalizer=canonicalizer, ledger=ledger, approvals=approvals,
            identity=identity, facts=facts, gate=gate, coordination=coordination,
            registry=registry, flags=flags, executor=executor, dispatcher=dispatcher)
    return ports


def _build_warehouse_executor(config) -> Callable[[str], Any]:
    """Platform seam: a blocking SQL executor bound to the provisioner identity.

    Constructs an explicit-credential Databricks client lazily (on first call)
    and runs one statement on the configured warehouse, raising on any
    non-succeeded state. Never offline-exercised; the wiring that consumes it
    (`resolve_provision_ports`) is tested with a fake `execute`.
    """

    warehouse_id = config["warehouse_id"]
    state: dict[str, Any] = {}

    def execute(statement: str) -> None:
        client = state.get("client")
        if client is None:
            from databricks.sdk import WorkspaceClient

            client = WorkspaceClient()
            state["client"] = client
        response = client.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=statement, wait_timeout="50s")
        status = response.status
        if status is None or status.state is None or status.state.value != "SUCCEEDED":
            raise PermissionError(f"Provisioning statement failed: {statement}")

    return execute


def _governed_seam_pending(port: str) -> Callable[..., Any]:
    """A fail-closed governed leaf constructor.

    The credential/policy-heavy governed factories (Delta-backed facts, the
    coordination service's admission/enrollment/recovery callbacks, the mutation
    gate graph, verified identity, explicit-credential SDK clients) are only
    built and validated against a provisioned namespace in the live integration
    phase. Until then each raises "not integrated" the first time
    `resolve_governed_ports` invokes it, keeping every governed kind fail-closed.
    """

    def factory(*_args, **_kwargs):
        raise PermissionError(
            f"VC governed runtime not integrated; the {port} platform seam is "
            "unavailable (provision and validate the target workspace first)")

    return factory


def _build_governed_seams(config, adapters=None) -> GovernedSeams:
    """Assemble the platform `GovernedSeams` for governed mutation kinds.

    The **promotion** graph is wired with real explicit-credential leaf
    constructors via `platform.live_seams.build_governed_seams` (Delta facts/
    ledger/registry/coordination, the mutation gate, `GenieTransport`, the
    approval service and the promotion subgraph). `restore`/`reconcile` remain
    unwired (their seams are `None`, so `resolve_governed_ports` skips them and
    those kinds stay fail-closed until wired and platform-validated).

    `adapters` is the low-level credential/SQL/Volume injection seam; production
    passes `None` (a live `PlatformAdapters(config)` is built from the explicit
    config), while offline composition tests inject a fake with the same surface.
    Falls back to the fail-closed placeholder graph only when explicit config is
    absent (so `build_vc_runtime` still raises "not integrated" pre-provisioning).
    """

    if not config or any(not config.get(key) for key in _GOVERNED_CONFIG_KEYS):
        names = ("facts", "ledger", "registry", "canonicalizer", "identity", "executor",
                 "coordination", "approvals", "transport", "gate", "capability_probe",
                 "dispatcher", "restore", "promotion", "reconcile")
        return GovernedSeams(**{name: _governed_seam_pending(name) for name in names})

    from backend.services.version_control.platform.live_seams import (
        build_governed_seams,
    )

    return build_governed_seams(config, adapters=adapters)


def _resolve_platform_ports(kind: str, config=None, adapters=None) -> dict:
    """Construct durable target-local ports for `kind` from explicit workspace
    configuration and credentials.

    `provision` is integrated: it resolves the reviewed owner migrations + grant
    matrix and a warehouse-bound executor from explicit config. Every governed
    mutation kind is routed through the completed composition path
    (`resolve_governed_ports` over `_build_governed_seams`); the leaf factories
    remain fail-closed platform seams until the live integration phase, so
    governed kinds still raise "not integrated" here.
    """

    if kind == "provision":
        complete = bool(config) and all(config.get(key) for key in _PROVISION_CONFIG_KEYS)
        # resolve_provision_ports fails closed on incomplete config before the
        # executor is ever used, so a no-op executor is safe in that case.
        execute = _build_warehouse_executor(config) if complete else (lambda _s: None)
        return resolve_provision_ports(config=config or {}, execute=execute)
    return resolve_governed_ports(config=config, seams=_build_governed_seams(config, adapters))


def build_vc_runtime(kind: str, config=None, adapters=None) -> VcJobRuntime:
    if kind not in JOB_KINDS:
        raise PermissionError(f"VC {kind} runtime not integrated; unknown job kind")
    ports = _resolve_platform_ports(kind, config, adapters)
    if kind == "provision":
        return assemble_provision_runtime(**ports)
    return assemble_vc_runtime(**ports)
