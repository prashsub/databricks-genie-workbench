"""Live governed-seam factories: the real credential/policy-heavy leaf
constructors for the promotion mutation graph.

`build_governed_seams(config, adapters=...)` returns a `GovernedSeams` whose
factory bodies build the durable, target-local leaf ports (Delta facts/ledger/
registry/coordination, the mutation gate, `GenieTransport`, the approval service,
the promotion subgraph). The composition *order* and fail-closed config gating
live in `backend/jobs.resolve_governed_ports`; these factories only construct the
leaves from explicit config plus a low-level `adapters` object.

`adapters` is the single injection seam. In a real Job it is `PlatformAdapters`,
which binds SQL/Volume/SDK access to the explicit role credentials. Offline tests
inject a fake with the same surface, so the whole graph can be assembled and
type-checked with no live platform (the leaves are the real classes; only the
lowest-level clients are faked).

Two composition subtleties are handled here:

* **Late-bound approvals.** `resolve_governed_ports` builds the coordination
  service before the approval service, but the coordination admission policy must
  re-validate the *same* approval grant. The factories share a per-build ``state``
  dict; the approvals factory records the built service and the coordination
  ``validate_authorization`` callback reads it at call time (after the whole graph
  is assembled), so ordering never forces two approval instances.
* **Dual transport.** The gate and observer drive the Genie write/read API via
  ``GenieTransport``; the promotion service additionally holds a *verifier*
  transport (``NativeTransport``) whose ``verify()`` asserts the cross-workspace
  read-only topology. The promotion factory builds the verifier itself and
  ignores the Genie transport handed in for the gate.

Scope: this build wires the **promotion** kind only (per the two-workspace gate).
``restore``/``optimizer``/``reconcile`` seams are left ``None`` so
`resolve_governed_ports` skips them and those kinds stay fail-closed until wired
and platform-validated.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from backend.services.config_fingerprint import Canonicalizer
from backend.services.genie_client import GenieTransport
from backend.services.version_control import contracts as vc
from backend.services.version_control.coordination import policies
from backend.services.version_control.coordination.service import CoordinationService
from backend.services.version_control.coordination.store import DeltaCoordinationStore
from backend.services.version_control.governance.approvals import ApprovalService
from backend.services.version_control.governance.delta import DeltaFactStore
from backend.services.version_control.governance.facts import DurableOperationFacts
from backend.services.version_control.ledger import DeltaVersionLedger
from backend.services.version_control.mutation_gate import MutationGate
from backend.services.version_control.observer import Observer
from backend.services.version_control.platform.termination import (
    PlatformTerminationEvidenceProvider,
)
from backend.services.version_control.promotion import PromotionService
from backend.services.version_control.promotion.dependencies import DependencyChecker
from backend.services.version_control.promotion.dispatch import PendingOperations
from backend.services.version_control.promotion.mapping import MappingTransformer
from backend.services.version_control.promotion.releases import ReleaseCatalog
from backend.services.version_control.promotion.tests import PreflightTestRunner
from backend.services.version_control.promotion.transport import NativeTransport
from backend.services.version_control.registry import DeltaRegistry


class _Clock:
    """Deployment-owned wall clock; never derived from request evidence.

    The durable subsystem injects one clock into consumers with two contracts:
    ``CoordinationService`` calls ``clock.now()`` while the durable stores
    (``DeltaCoordinationStore``/``DeltaRegistry``) and ``DriftService`` call
    ``clock()`` (offline they pass ``lambda: NOW``). This adapter satisfies both so
    a single instance can back the whole governed graph.
    """

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now()


def _qualified(config: dict, table: str) -> str:
    return f"`{config['catalog']}`.`{config['control_schema']}`.`{table}`"


def _selection(spec: dict) -> vc.ExplicitExecutorSelection:
    return vc.ExplicitExecutorSelection(
        spec["workspace_id"], spec["host"], spec["principal_id"],
        spec["execution_ref"], spec.get("profile"))


def build_governed_seams(config: dict, *, adapters: Any = None):
    """Assemble the real `GovernedSeams` for the governed promotion graph.

    `adapters` defaults to a live `PlatformAdapters(config)`; tests inject a fake
    with the same low-level surface (`sql`, `files_store`, `authenticate`,
    `read_evidence`, `identity_provider`, `capability_probe`, `topology_proof`,
    `dispatcher`).
    """
    from backend.jobs import GovernedSeams  # local import avoids a cycle

    if adapters is None:
        adapters = PlatformAdapters(config)

    clock = _Clock()
    # Shared, per-build namespace for late-binding (approvals -> coordination).
    state: dict[str, Any] = {}

    def facts_factory(cfg):
        store = DeltaFactStore(adapters.sql("executor"), cfg["catalog"], cfg["control_schema"],
                               cfg["workspace_id"], writes_enabled=True,
                               read_evidence=adapters.read_evidence)
        state["fact_store"] = store
        return DurableOperationFacts(store.read, store.append, writes_enabled=True)

    def ledger_factory(cfg):
        snapshots = adapters.files_store("executor")
        return DeltaVersionLedger(
            adapters.sql("executor"), cfg["catalog"], cfg["control_schema"], cfg["workspace_id"],
            write_artifact=snapshots.put_if_absent, read_artifact=snapshots.read,
            writes_enabled=True)

    def registry_factory(cfg):
        # Enrollment (initialize/serialize_enrollment) is performed by the separate
        # provisioning step, not the promotion Job; the target binding is already
        # enrolled, so the runtime registry is read/resolve only.
        return DeltaRegistry(adapters.sql("executor"), cfg["catalog"], cfg["control_schema"],
                             cfg["workspace_id"], clock=clock, writes_enabled=False)

    def canonicalizer_factory(_cfg):
        return Canonicalizer()

    def identity_factory(cfg):
        identity = adapters.identity_provider("executor")
        state["identity"] = identity
        return identity

    def executor_factory(cfg, *, identity):
        executor = identity.executor(_selection(cfg["target_selection"]))
        state["executor"] = executor
        return executor

    def coordination_factory(cfg, *, facts, ledger, registry, canonicalizer):
        store = DeltaCoordinationStore(
            adapters.sql("executor"), _qualified(cfg, "genie_ops_coordination"),
            resolve_binding=policies.resolve_binding(registry), clock=clock)
        state["coordination_store"] = store
        termination = PlatformTerminationEvidenceProvider(
            adapters.attempt_inventory, adapters.job_reader, adapters.worker_supervisor_reader)
        service = CoordinationService(
            store=store, facts=facts, ledger=ledger, clock=clock,
            resolve_binding=policies.resolve_binding(registry),
            verify_enrollment=policies.verify_enrollment(registry),
            insert_enrolled=policies.insert_enrolled(store),
            # Late-bound: the approval service is built after coordination but the
            # grant is only re-validated at admission time, once the graph exists.
            validate_authorization=lambda grant, executor: policies.validate_authorization(
                state["approvals"])(grant, executor),
            verify_create_intent=policies.verify_create_intent(facts),
            termination=termination,
            authorize_human_recovery=policies.authorize_human_recovery,
            authorize_heads=policies.authorize_heads,
            validate_stage_evidence=policies.validate_stage_evidence)
        # The Observer (built later in the promotion graph) drives leases/head
        # advancement through the *service* (observe_exclusively/advance_heads live
        # here, not on the store), so stash the shared instance for it.
        state["coordination_service"] = service
        return service

    def approvals_factory(cfg, *, facts, identity, registry):
        approvals = ApprovalService(facts, identity, registry, clock.now,
                                    target_identity=lambda _selection: identity,
                                    writes_enabled=True)
        state["approvals"] = approvals
        return approvals

    def transport_factory(cfg, *, executor, coordination, registry, flags):
        transport = GenieTransport(
            executor=executor, authenticate=adapters.authenticate, coordination=coordination,
            registry=registry, flags=flags,
            warehouse_id=cfg.get("target_warehouse_id") or cfg.get("warehouse_id"),
            parent_path=cfg.get("target_parent_path"))
        state["genie_transport"] = transport
        return transport

    def gate_factory(*, coordination, ledger, facts, approvals, transport, canonicalizer,
                     flags, registry):
        return MutationGate(coordination=coordination, ledger=ledger, facts=facts,
                            approvals=approvals, transport=transport,
                            canonicalizer=canonicalizer, flags=flags, registry=registry)

    def capability_probe_factory(cfg):
        return adapters.capability_probe()

    def dispatcher_factory(cfg, *, identity, executor, facts):
        return adapters.dispatcher()

    def promotion_factory(cfg, *, facts, gate, approvals, identity, ledger, canonicalizer,
                          registry, transport, flags, executor, dispatcher):
        volumes = cfg["volumes"]
        # Verifier transport (topology assertion) is distinct from the gate's
        # Genie write transport handed in as `transport`.
        native = NativeTransport(adapters.topology_proof, cfg["source_workspace_id"],
                                 cfg["workspace_id"])
        observer = Observer(
            coordination=state["coordination_service"], ledger=ledger, canonicalizer=canonicalizer,
            transport=transport, identity=identity,
            reader_selection=_selection(cfg["target_selection"]),
            status_reader=_clean_status_reader(state["coordination_store"], registry))
        ports = {
            "workspace_id": cfg["workspace_id"],
            "target_host": cfg["target_host"],
            "target_selection": _selection(cfg["target_selection"]),
            "source_selection": _selection(cfg["source_selection"]),
            "source_binding": vc.from_wire(vc.BindingRef, cfg["source_binding"]),
            "source_identity": adapters.identity_provider("source"),
            "identity": identity,
            "flags": flags, "facts": facts, "ledger": ledger, "registry": registry,
            "canonicalizer": canonicalizer, "gate": gate, "approvals": approvals,
            "dispatcher": dispatcher, "transport": native,
            "transformer": MappingTransformer(),
            "store": adapters.files_store("source"),
            "receipt_store": adapters.files_store("executor"),
            "dependencies": adapters.dependency_checker(),
            "tests": PreflightTestRunner(query=adapters.sql("executor")),
            "releases": ReleaseCatalog(adapters.release_facts_reader(facts),
                                       volumes["outbound"], cfg["target_host"]),
            "pending": PendingOperations(adapters.pending_reader(facts)),
            "observer": observer,
            "outbound_volume": volumes["outbound"],
            "approval_volume": volumes["approval"],
            "receipt_volume": volumes["receipts"],
            "reverse_receipt_volume": cfg.get("reverse_receipt_volume"),
        }
        return PromotionService(**ports)

    return GovernedSeams(
        facts=facts_factory, ledger=ledger_factory, registry=registry_factory,
        canonicalizer=canonicalizer_factory, identity=identity_factory,
        executor=executor_factory, coordination=coordination_factory,
        approvals=approvals_factory, transport=transport_factory, gate=gate_factory,
        capability_probe=capability_probe_factory, dispatcher=dispatcher_factory,
        promotion=promotion_factory,
        restore=None, optimizer=None, reconcile=None)


def _clean_status_reader(coordination_store, registry) -> Callable[[Any, Any], Any]:
    """Target-local single-writer status reader for the promotion observer.

    Reports drift ``CLEAN`` and not-stale, deriving only quarantine / unresolved
    operation / heads from the durable coordination row (fail-closed to the row's
    own state). It never fabricates optimistic drift: the observer independently
    compares the live GET against the ledger head, so a real divergence still
    surfaces through the gate's read-back checks.
    """

    def read(binding, _actor):
        now = datetime.now(UTC)
        try:
            row = coordination_store.read(binding.binding_id)
        except Exception:  # noqa: BLE001 - coordination outage -> fail-closed stale
            return vc.BindingStatus(
                binding_id=binding.binding_id, binding_revision=binding.binding_revision,
                heads=vc.Heads(None, None, None), drift=vc.DriftState.UNKNOWN,
                quarantined=True, unresolved_operation_id=None, observed_at=None,
                projection_as_of=now, stale=True, allowed_actions=(),
                reasons=("Coordination authority unavailable",))
        if row is None:
            return vc.BindingStatus(
                binding_id=binding.binding_id, binding_revision=binding.binding_revision,
                heads=vc.Heads(None, None, None), drift=vc.DriftState.CLEAN,
                quarantined=False, unresolved_operation_id=None, observed_at=None,
                projection_as_of=now, stale=False, allowed_actions=(), reasons=())
        quarantined = bool(getattr(row, "quarantine_reason", None))
        unresolved = getattr(row, "active_operation_id", None) if getattr(row, "unresolved", False) else None
        return vc.BindingStatus(
            binding_id=binding.binding_id, binding_revision=binding.binding_revision,
            heads=getattr(row, "heads", None) or vc.Heads(None, None, None),
            drift=vc.DriftState.CLEAN, quarantined=quarantined,
            unresolved_operation_id=unresolved, observed_at=getattr(row, "updated_at", None),
            projection_as_of=now, stale=False, allowed_actions=(), reasons=())

    return read


class PlatformAdapters:
    """Live, credential-bound low-level access for the governed seams.

    Every method binds SQL/Volume/SDK access to one explicit role's OAuth
    credentials from `config["roles"][role]` (profile + warehouse + host). This is
    never exercised offline; the seams that consume it are validated against fakes,
    and this class is validated on the live platform.
    """

    def __init__(self, config: dict):
        self.config = config
        self._clients: dict[str, Any] = {}

    # -- SDK clients (explicit role credentials) --------------------------
    def client(self, role: str):
        if role not in self._clients:
            import os

            from databricks.sdk import WorkspaceClient

            selection = self.config["roles"][role]
            profile = selection.get("profile")
            saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("DATABRICKS_")}
            try:
                kwargs: dict[str, Any] = {}
                if profile:
                    kwargs["profile"] = profile
                if self.config.get("config_file"):
                    kwargs["config_file"] = self.config["config_file"]
                self._clients[role] = WorkspaceClient(**kwargs)
            finally:
                os.environ.update(saved)
        return self._clients[role]

    # -- SQL (REST Statements API; thrift is IP-ACL blocked) --------------
    def sql(self, role: str) -> Callable[..., list]:
        import time

        warehouse = self.config["roles"][role]["warehouse_id"]
        client = self.client(role)

        def execute(statement: str, parameters: Any = None) -> list:
            body: dict[str, Any] = {"warehouse_id": warehouse, "statement": statement,
                                    "wait_timeout": "30s", "on_wait_timeout": "CONTINUE"}
            rendered = _statement_parameters(parameters)
            if rendered:
                body["parameters"] = rendered
            response = client.api_client.do("POST", "/api/2.0/sql/statements", body=body)
            deadline = time.monotonic() + 180
            while response["status"]["state"] in {"PENDING", "RUNNING"}:
                if time.monotonic() >= deadline:
                    raise RuntimeError("SQL statement timed out")
                time.sleep(2)
                response = client.api_client.do(
                    "GET", f"/api/2.0/sql/statements/{response['statement_id']}")
            if response["status"]["state"] != "SUCCEEDED":
                error = response["status"].get("error", {})
                raise RuntimeError(f"{error.get('error_code', 'SQL_FAILED')}: {error.get('message', '')}")
            return _rows(response)

        return execute

    # -- Volume files (content-addressed store) ---------------------------
    def files_store(self, role: str):
        from backend.services.version_control.promotion.store import files_volume_store

        return files_volume_store(self.client(role))

    def read_evidence(self, uri: str) -> bytes:
        return self.client("approval").files.download(uri).contents.read()

    # -- explicit-executor authentication for GenieTransport --------------
    def authenticate(self, _executor) -> dict:
        return self.client("executor").config.authenticate()

    # -- identity provider (SCIM-backed, per role) ------------------------
    def identity_provider(self, role: str):
        from backend.services.version_control.platform.identity import (
            PlatformIdentityProvider,
        )

        selection = self.config["roles"][role]
        profile = selection.get("profile")
        directory = self.client(self.config.get("directory_role", "executor"))

        def resolve_groups(subject_id, _workspace_id):
            try:
                user = directory.users.get(subject_id)
                return frozenset(group.display for group in (user.groups or []))
            except Exception:  # noqa: BLE001 - SCIM user miss -> SP lookup
                for sp in directory.service_principals.list(
                        filter=f'applicationId eq "{subject_id}"'):
                    return frozenset(group.display for group in (sp.groups or []))
            raise PermissionError(f"Unknown subject for group resolution: {subject_id}")

        def resolve_actor(reference):
            try:
                directory.users.get(reference)
                kind = "human"
            except Exception:  # noqa: BLE001 - SCIM user miss -> SP
                if not list(directory.service_principals.list(
                        filter=f'applicationId eq "{reference}"')):
                    raise PermissionError(f"Unknown authenticated subject: {reference}")
                kind = "service"
            return vc.ActorContext(reference, selection["workspace_id"], kind)

        _EDIT_LEVELS = frozenset({"CAN_EDIT", "CAN_MANAGE", "IS_OWNER"})

        def resolve_edit(subject_id, binding):
            # A release requester must genuinely hold edit rights on the target
            # Genie space. Reading the object ACL requires CAN_MANAGE, which the
            # executor already holds, so this works both in the driver (admin
            # directory) and in the Job (executor directory).
            if not getattr(binding, "space_id", None):
                return False
            acl = directory.api_client.do(
                "GET", f"/api/2.0/permissions/genie/{binding.space_id}")
            for entry in acl.get("access_control_list", []) or []:
                ref = (entry.get("service_principal_name") or entry.get("user_name")
                       or entry.get("group_name"))
                if ref == subject_id and any(
                        perm.get("permission_level") in _EDIT_LEVELS
                        for perm in entry.get("all_permissions", []) or []):
                    return True
            return False

        return PlatformIdentityProvider(
            profiles={profile: (lambda role=role: self.client(role))} if profile else None,
            request_resolver=resolve_actor, group_resolver=resolve_groups,
            edit_resolver=resolve_edit, job_client=self.client(role))

    # -- capability + topology proofs -------------------------------------
    def capability_probe(self) -> Callable[[], dict]:
        proof = self.config.get("capabilities", {})
        return lambda: dict(proof)

    def topology_proof(self) -> dict:
        return dict(self.config["topology"])

    # -- termination readers (positive termination only) ------------------
    def attempt_inventory(self, execution_ref, attempt_id):
        return None

    def job_reader(self, execution_ref, attempt_id):
        return None

    def worker_supervisor_reader(self, execution_ref, attempt_id):
        return None

    # -- dependency checker (live preflight permission probes) ------------
    def dependency_checker(self) -> DependencyChecker:
        client = self.client("executor")

        def grantees(kind, identifier, privilege):
            return frozenset()  # populated live via SHOW GRANTS; wired in D2.5

        def warehouse_users(identifier):
            return frozenset()

        def folder_users(identifier):
            return frozenset()

        def resolve_groups(principal):
            try:
                user = client.users.get(principal)
                return frozenset(group.display for group in (user.groups or []))
            except Exception:  # noqa: BLE001
                return frozenset()

        return DependencyChecker(grantees=grantees, warehouse_users=warehouse_users,
                                 folder_users=folder_users, resolve_groups=resolve_groups)

    # -- projections over the durable operations facts --------------------
    def release_facts_reader(self, facts) -> Callable[[str], list]:
        def read(operation_id):
            operation = facts.get_request(operation_id)
            release = getattr(operation, "release", None)
            return list(release or [])

        return read

    def pending_reader(self, facts) -> Callable[..., Any]:
        def read(workspace_id, cursor):
            return vc.Page((), None)

        return read

    def dispatcher(self):
        config = self.config
        client = self.client("executor")

        class _Dispatcher:
            def submit_local(self, operation_id, job_kind):
                run = client.jobs.run_now(job_id=config["promotion_job_id"],
                                          job_parameters={"operation_id": operation_id})
                return vc.OperationHandle(operation_id, vc.OperationStatus.REQUESTED,
                                          str(run.run_id))

        return _Dispatcher()


def _statement_parameters(parameters):
    """Render a named-parameter dict into the SQL Statements API list with
    explicit types (mirrors the integration harness renderer)."""
    rendered = []
    for name, value in (parameters or {}).items():
        if value is None:
            rendered.append({"name": name, "value": None})
        elif isinstance(value, bool):
            rendered.append({"name": name, "value": str(value).lower(), "type": "BOOLEAN"})
        elif isinstance(value, int):
            rendered.append({"name": name, "value": str(value), "type": "BIGINT"})
        elif name.endswith("_at"):
            moment = datetime.fromisoformat(str(value))
            rendered.append({"name": name, "value": moment.strftime("%Y-%m-%d %H:%M:%S.%f"),
                             "type": "TIMESTAMP"})
        else:
            rendered.append({"name": name, "value": str(value), "type": "STRING"})
    return rendered


_INTEGER_TYPES = frozenset({"BYTE", "SHORT", "INT", "INTEGER", "LONG", "BIGINT"})
_FLOAT_TYPES = frozenset({"FLOAT", "DOUBLE"})


def _coerce(type_name: str, value):
    """Coerce a SQL Statements API string cell into the store's native Python type.

    The REST Statements API returns every cell as a string (thrift is IP-ACL
    blocked), but the durable stores compare typed values — integer fences
    (`binding_revision`, `row_version`, `generation`), booleans and `datetime`
    timestamps. Coercing here from the result manifest's `type_name` makes the REST
    seam behave like the typed connector the M03 CAS gate validated against; string
    columns (e.g. SHOW GRANTS) pass through unchanged.
    """
    if value is None:
        return None
    if type_name in _INTEGER_TYPES:
        return int(value)
    if type_name == "BOOLEAN":
        return value == "true"
    if type_name in _FLOAT_TYPES:
        return float(value)
    if type_name.startswith("DECIMAL"):
        return float(value)
    if type_name.startswith("TIMESTAMP"):
        return datetime.fromisoformat(value.replace(" ", "T", 1))
    return value


def _rows(response) -> list:
    manifest = response.get("manifest", {}) or {}
    schema_columns = (manifest.get("schema", {}) or {}).get("columns", [])
    names = [c["name"] for c in schema_columns]
    types = [c.get("type_name", "STRING") for c in schema_columns]
    data = (response.get("result", {}) or {}).get("data_array", []) or []
    return [{name: _coerce(type_name, cell)
             for name, type_name, cell in zip(names, types, row, strict=False)}
            for row in data]
