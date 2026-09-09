"""Optimizer orchestration over injected VC/1.0 ports, never a version ledger.

No backend application imports: the target-local composition owns adapters.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol


def require_candidate_session_for_job():
    raise PermissionError(
        "M10 deployment blocker: no target-local CandidateSession lifecycle is installed; "
        "QC benchmark publication and optimization/enrichment writes are disabled"
    )


class IsolationRegistry(Protocol):
    def resolve_physical(self, workspace_id: str, space_id: str) -> Any: ...
    def optimizer_owner(self, workspace_id: str, space_id: str) -> str | None: ...
    def workspace_id_for(self, client: Any) -> str: ...


@dataclass(frozen=True)
class EvaluationBinding:
    workspace_id: str
    space_id: str
    run_id: str

    def validate(self, registry: IsolationRegistry) -> "EvaluationBinding":
        if not all((self.workspace_id, self.space_id, self.run_id)):
            raise PermissionError("Evaluation resource identity is required")
        if registry.resolve_physical(self.workspace_id, self.space_id) is not None:
            raise PermissionError("Candidate evaluation cannot target a managed binding")
        if registry.optimizer_owner(self.workspace_id, self.space_id) != self.run_id:
            raise PermissionError("Positive optimizer resource ownership is required")
        return self


class CandidateSession:
    def __init__(self, resource: EvaluationBinding, registry: IsolationRegistry,
                 client: Any, *, writes_enabled: bool = False):
        if writes_enabled is not True:
            raise PermissionError("Optimizer candidate writes are disabled")
        self.resource = resource.validate(registry)
        if registry.workspace_id_for(client) != resource.workspace_id:
            raise PermissionError("Candidate client workspace does not match resource")
        self.registry = registry
        self.client = _EvaluationClient(client, self)

    def validate(self, run_id: str):
        if run_id != self.resource.run_id:
            raise PermissionError("Candidate belongs to another optimizer run")
        return self.resource.validate(self.registry)


class _EvaluationClient:
    def __init__(self, client: Any, session: CandidateSession):
        self._client = client
        self._session = session
        self.api_client = self

    def __getattr__(self, name):
        if name == "genie":
            from databricks.sdk.service.dashboards import GenieAPI
            return GenieAPI(self)
        if name == "config":
            return self._client.config
        raise PermissionError(f"Candidate client does not expose {name}")

    def do(self, method, path, **kwargs):
        resource = self._session.validate(self._session.resource.run_id)
        prefix = f"/api/2.0/genie/spaces/{resource.space_id}"
        if path != prefix and not path.startswith(prefix + "/"):
            raise PermissionError("Candidate API call escaped isolated resource")
        if any(part in path for part in ("..", "%", "?", "#")):
            raise PermissionError("Noncanonical candidate API path")
        return self._client.api_client.do(method, path, **kwargs)


def assert_candidate_write(client: Any, space_id: str) -> None:
    if not isinstance(client, _EvaluationClient):
        raise PermissionError("Managed writes require the champion gate; candidate proof is missing")
    resource = client._session.validate(client._session.resource.run_id)
    if resource.space_id != space_id:
        raise PermissionError("Mutation does not target the isolated candidate resource")


class OptimizerIsolationRegistry:
    """Concrete :class:`IsolationRegistry`.

    Managed-binding detection is delegated to an injected read of the VC
    managed registry (``managed_lookup``); optimizer ownership of candidate
    spaces is tracked in-process and only ever set/cleared by the candidate
    lifecycle. It refuses to claim a space the managed registry reports as a
    live binding (defense in depth against cloning onto a governed space).
    """

    def __init__(self, managed_lookup, workspace_resolver):
        self._managed_lookup = managed_lookup
        self._workspace_resolver = workspace_resolver
        self._owners: dict[tuple[str, str], str] = {}
        self._lock = RLock()

    def resolve_physical(self, workspace_id: str, space_id: str) -> Any:
        return self._managed_lookup(workspace_id, space_id)

    def optimizer_owner(self, workspace_id: str, space_id: str) -> str | None:
        with self._lock:
            return self._owners.get((workspace_id, space_id))

    def workspace_id_for(self, client: Any) -> str:
        return self._workspace_resolver(client)

    def claim(self, workspace_id: str, space_id: str, run_id: str) -> None:
        if self.resolve_physical(workspace_id, space_id) is not None:
            raise PermissionError("Cannot claim a managed binding as a candidate")
        with self._lock:
            existing = self._owners.get((workspace_id, space_id))
            if existing is not None and existing != run_id:
                raise PermissionError("Candidate is already owned by another optimizer run")
            self._owners[(workspace_id, space_id)] = run_id

    def release(self, workspace_id: str, space_id: str, run_id: str) -> None:
        with self._lock:
            existing = self._owners.get((workspace_id, space_id))
            if existing is not None and existing != run_id:
                raise PermissionError("Candidate is not owned by this optimizer run")
            self._owners.pop((workspace_id, space_id), None)


class CandidateSpaceTransport(Protocol):
    def create_candidate(self, workspace_id: str, source_space_id: str, run_id: str) -> str:
        """Provision an isolated candidate copy of the managed source; return its id."""
        ...

    def delete_candidate(self, workspace_id: str, space_id: str) -> None:
        """Tear down a candidate space; must tolerate best-effort repeated calls."""
        ...


class CandidateSpaceLifecycle:
    """Create/own/tear-down an optimizer-owned candidate Genie space, fail-closed.

    Writes are disabled by default. The source must be a managed live binding
    (we only clone a governed space). Ownership is claimed before the session is
    yielded and always revoked on exit; the candidate is always deleted, and a
    failed delete still revokes ownership so a leaked id cannot be reused
    silently.
    """

    def __init__(self, registry: OptimizerIsolationRegistry,
                 transport: CandidateSpaceTransport, *, writes_enabled: bool = False):
        self._registry = registry
        self._transport = transport
        self._writes_enabled = writes_enabled

    @contextmanager
    def session(self, run_id: str, workspace_id: str, source_space_id: str, client: Any):
        if self._writes_enabled is not True:
            raise PermissionError("Candidate lifecycle writes are disabled")
        if self._registry.resolve_physical(workspace_id, source_space_id) is None:
            raise PermissionError("Source binding is not a managed live space")
        candidate_id = self._transport.create_candidate(workspace_id, source_space_id, run_id)
        try:
            self._registry.claim(workspace_id, candidate_id, run_id)
        except BaseException:
            self._transport.delete_candidate(workspace_id, candidate_id)
            raise
        binding = EvaluationBinding(workspace_id, candidate_id, run_id)
        session = CandidateSession(binding, self._registry, client, writes_enabled=True)
        try:
            yield session
        finally:
            self._registry.release(workspace_id, candidate_id, run_id)
            self._transport.delete_candidate(workspace_id, candidate_id)


class ChampionAdapter(Protocol):
    def apply(self, run_id: str, champion_id: str, binding: Any,
              expected_base: str, executor: Any) -> Any: ...


@dataclass(frozen=True)
class RecoveryHandle:
    operation_id: str
    job_kind: str = "recovery"


@dataclass(frozen=True)
class RestoreSelection:
    run_id: str
    historical_version_id: str
    binding: Any
    expected_base: str
    approval_id: str


class RestoreJobs(Protocol):
    def submit(self, selection: RestoreSelection, requester: Any) -> Any:
        """Resolve immutable approved restore inputs and submit through M04."""
        ...


@dataclass(frozen=True)
class OptimizerRuntime:
    """Entry point for explicit M08 target-local composition, not backend startup."""

    contract_version = "VC/1.0"
    champion_adapter: ChampionAdapter
    restore_jobs: RestoreJobs
    isolation_registry: IsolationRegistry

    def champion_run(self, run_id: str) -> "ChampionRun":
        return ChampionRun(run_id, self.champion_adapter)


class ChampionRun:
    def __init__(self, run_id: str, adapter: ChampionAdapter):
        self.run_id = run_id
        self.adapter = adapter
        self.iterations = []
        self.abandoned = False
        self.receipt = None
        self.post_version = None
        self.pre_version = None
        self.evidence_references = ()
        self.recovery_handle = None
        self._apply_identity = None

    def record_iteration(self, champion_id: str, payload: dict, score: float):
        from copy import deepcopy
        self.iterations.append((champion_id, deepcopy(payload), score))

    def abandon(self):
        self.abandoned = True

    def apply(self, champion_id, binding, expected_base, executor):
        if self.abandoned:
            raise PermissionError("An abandoned run cannot apply a champion")
        identity = (champion_id, binding, expected_base)
        if self._apply_identity is not None:
            if identity != self._apply_identity:
                raise ValueError("Run already selected a different champion/base")
            if self.receipt is None:
                raise RuntimeError("Unknown apply outcome requires governed Job recovery")
            return self.receipt
        self._apply_identity = identity
        receipt = self.adapter.apply(self.run_id, champion_id, binding, expected_base, executor)
        self.receipt = receipt
        self.pre_version = receipt.preimage
        self.evidence_references = receipt.evidence_references
        if receipt.unresolved or receipt.status in {"applied_partial", "applied_unverified", "quarantined"}:
            self.recovery_handle = RecoveryHandle(receipt.operation_id)
            return receipt
        if receipt.status == "noop" and not receipt.unresolved:
            self.post_version = None
            return receipt
        if receipt.status != "confirmed" or receipt.unresolved or receipt.postimage is None:
            raise RuntimeError("Champion has no verified final post-version")
        self.post_version = receipt.postimage
        return receipt
