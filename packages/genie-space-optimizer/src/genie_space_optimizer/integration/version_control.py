"""Optimizer orchestration over injected VC/1.0 ports, never a version ledger.

No backend application imports: the target-local composition owns adapters.
"""
from typing import Protocol, Any
from dataclasses import dataclass


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


class ChampionAdapter(Protocol):
    def apply(self, run_id: str, champion_id: str, binding: Any,
              expected_base: str, executor: Any) -> Any: ...


class ChampionRun:
    def __init__(self, run_id: str, adapter: ChampionAdapter):
        self.run_id = run_id
        self.adapter = adapter
        self.iterations = []
        self.abandoned = False
        self.receipt = None

    def record_iteration(self, champion_id: str, payload: dict, score: float):
        from copy import deepcopy
        self.iterations.append((champion_id, deepcopy(payload), score))

    def abandon(self):
        self.abandoned = True
