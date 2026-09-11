"""Observe-only runtime: the reads-plus-capture VC surface for the optimizer pivot.

The observe-only pivot governs *nothing* the optimizer does; it records what a space
looked like before and after an optimizer run so an operator can see the change history
and (later) restore. That needs a much smaller graph than the promotion gate:

* an **enroll-capable** ``DeltaRegistry`` (auto-enroll a space on first optimizer
  trigger) whose enrollment initializes the single coordination row,
* a writes-enabled ``DeltaVersionLedger`` (append observations, read history),
* a ``CoordinationService`` (the Observer takes exclusive observation leases and
  advances the observed head through the *service*, not the store),
* a ``GenieTransport`` (GET the live serialized space), a ``Canonicalizer``, and the
  target-workspace ``IdentityProvider``,
* an ``Observer`` bound to a target-local ``_clean_status_reader``.

It reuses the live promotion leaf classes and the ``adapters`` injection seam from
``live_seams`` verbatim, so offline the whole runtime assembles/type-checks against a
fake ``adapters`` (see ``test_vc_observe_seams``) and only the lowest-level clients are
live. Enrollment + observation are *writes* — gated behind ``vc_writes_enabled`` at the
call sites — but the history/status *reads* the routers expose require only
``vc_history_enabled``. ``resolve_observe_runtime`` is fully fail-closed: absent or
incomplete config yields ``None`` so the app mounts no observe surface (deploy-only
validated per the two-install-path contract).

Restore/reconcile mutations are intentionally NOT built here; they stay fail-closed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.services.config_fingerprint import Canonicalizer
from backend.services.genie_client import GenieTransport
from backend.services.version_control import contracts as vc
from backend.services.version_control.coordination import policies
from backend.services.version_control.coordination.service import CoordinationService
from backend.services.version_control.coordination.store import DeltaCoordinationStore
from backend.services.version_control.governance.delta import DeltaFactStore
from backend.services.version_control.governance.facts import DurableOperationFacts
from backend.services.version_control.ledger import DeltaVersionLedger
from backend.services.version_control.observer import Observer
from backend.services.version_control.platform.feature_flags import FeatureFlags
from backend.services.version_control.platform.live_seams import (
    PlatformAdapters,
    _clean_status_reader,
    _Clock,
    _qualified,
    _selection,
)
from backend.services.version_control.platform.termination import (
    PlatformTerminationEvidenceProvider,
)
from backend.services.version_control.registry import DeltaRegistry

_REQUIRED_CONFIG = ("workspace_id", "catalog", "control_schema", "target_selection")


@dataclass(frozen=True)
class ObserveRuntime:
    """The constructed observe/read ports the routers and the /trigger hook consume."""

    observer: Observer
    ledger: DeltaVersionLedger
    registry: DeltaRegistry
    facts: DurableOperationFacts
    identity: Any
    coordination: CoordinationService
    canonicalizer: Canonicalizer
    transport: GenieTransport
    reader_selection: vc.ExplicitExecutorSelection
    authorize_history: Callable[[Any, Any], bool]
    flags: FeatureFlags
    workspace_id: str
    actor: vc.ActorContext
    environment: str


def _deny_authorization(_grant: Any, _executor: Any) -> bool:
    # The observe runtime never admits a governed mutation (no gate is built here);
    # admission authorization is therefore fail-closed. observe_exclusively /
    # advance_heads / release_observation never reach this path.
    return False


def build_observe_runtime(config: dict, *, adapters: Any = None,
                          flags: FeatureFlags | None = None) -> ObserveRuntime:
    """Assemble the observe runtime from explicit config + an injectable ``adapters``.

    ``adapters`` defaults to a live ``PlatformAdapters(config)``; tests inject a fake
    with the same low-level surface. Raises ``PermissionError`` (fail-closed) when the
    trusted-workspace config is incomplete, mirroring ``build_vc_runtime``.
    """
    missing = [key for key in _REQUIRED_CONFIG if not config.get(key)]
    if missing:
        raise PermissionError(f"Observe runtime not integrated: missing config {missing}")
    if adapters is None:
        adapters = PlatformAdapters(config)
    flags = flags if flags is not None else FeatureFlags.from_config(config.get("flags", {}))

    clock = _Clock()
    catalog, control_schema = config["catalog"], config["control_schema"]
    workspace_id = config["workspace_id"]
    sql = adapters.sql("executor")
    selection = _selection(config["target_selection"])

    identity = adapters.identity_provider("executor")
    canonicalizer = Canonicalizer()

    # Registry <-> coordination store are mutually referential: the store resolves a
    # binding through the registry, and enrollment initializes the store's single row.
    holder: dict[str, DeltaRegistry] = {}
    coordination_store = DeltaCoordinationStore(
        sql, _qualified(config, "genie_ops_coordination"),
        resolve_binding=lambda binding_id: holder["registry"].resolve(binding_id), clock=clock)
    registry = DeltaRegistry(
        sql, catalog, control_schema, workspace_id, clock=clock, writes_enabled=True,
        initialize=coordination_store.initialize)
    holder["registry"] = registry

    fact_store = DeltaFactStore(sql, catalog, control_schema, workspace_id,
                                writes_enabled=True, read_evidence=adapters.read_evidence)
    facts = DurableOperationFacts(fact_store.read, fact_store.append, writes_enabled=True)

    snapshots = adapters.files_store("executor")
    ledger = DeltaVersionLedger(
        sql, catalog, control_schema, workspace_id,
        write_artifact=snapshots.put_if_absent, read_artifact=snapshots.read,
        writes_enabled=True)

    termination = PlatformTerminationEvidenceProvider(
        adapters.attempt_inventory, adapters.job_reader, adapters.worker_supervisor_reader)
    coordination = CoordinationService(
        store=coordination_store, facts=facts, ledger=ledger, clock=clock,
        resolve_binding=policies.resolve_binding(registry),
        verify_enrollment=policies.verify_enrollment(registry),
        insert_enrolled=policies.insert_enrolled(coordination_store),
        validate_authorization=_deny_authorization,
        verify_create_intent=policies.verify_create_intent(facts),
        termination=termination,
        authorize_human_recovery=policies.authorize_human_recovery,
        authorize_heads=policies.authorize_heads,
        validate_stage_evidence=policies.validate_stage_evidence)

    executor = identity.executor(selection)
    transport = GenieTransport(
        executor=executor, authenticate=adapters.authenticate, coordination=coordination,
        registry=registry, flags=flags,
        warehouse_id=config.get("target_warehouse_id") or config.get("warehouse_id"),
        parent_path=config.get("target_parent_path"))

    observer = Observer(
        coordination=coordination, ledger=ledger, canonicalizer=canonicalizer,
        transport=transport, identity=identity, reader_selection=selection,
        status_reader=_clean_status_reader(coordination_store, registry))

    actor = vc.ActorContext(config["target_selection"]["principal_id"], workspace_id, "service")

    def authorize_history(actor_ctx: Any, binding: Any) -> bool:
        # Target-local read authorization: the caller must resolve into the trusted
        # target workspace (OBO reads are proxied through the SP actor by the router).
        return actor_ctx.workspace_id == binding.workspace_id == workspace_id

    return ObserveRuntime(
        observer=observer, ledger=ledger, registry=registry, facts=facts, identity=identity,
        coordination=coordination, canonicalizer=canonicalizer, transport=transport,
        reader_selection=selection, authorize_history=authorize_history, flags=flags,
        workspace_id=workspace_id, actor=actor,
        environment=str(config.get("environment") or "prod"))


def resolve_observe_runtime(config: dict | None, *, adapters: Any = None,
                            flags: FeatureFlags | None = None) -> ObserveRuntime | None:
    """Fail-closed app-startup entry: return ``None`` when the observe runtime cannot be
    integrated (no/incomplete config, or any construction failure), so the app mounts no
    observe surface rather than a half-wired one."""
    if not config:
        return None
    try:
        return build_observe_runtime(config, adapters=adapters, flags=flags)
    except Exception:  # noqa: BLE001 - any assembly failure -> no observe surface (fail-closed)
        return None
