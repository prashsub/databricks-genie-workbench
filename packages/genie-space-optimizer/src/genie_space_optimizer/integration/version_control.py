"""Optimizer orchestration over injected VC/1.0 ports, never a version ledger.

No backend application imports: the target-local composition owns adapters.
"""
from typing import Protocol, Any


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
