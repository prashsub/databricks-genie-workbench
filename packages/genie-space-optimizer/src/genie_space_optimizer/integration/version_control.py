"""Optimizer orchestration over injected VC/1.0 ports, never a version ledger.

No backend application imports: the target-local composition owns adapters.
"""
from typing import Protocol, Any
from dataclasses import dataclass


class IsolationRegistry(Protocol):
    def resolve_physical(self, workspace_id: str, space_id: str) -> Any: ...
    def optimizer_owner(self, workspace_id: str, space_id: str) -> str | None: ...


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
