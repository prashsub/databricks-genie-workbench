"""Warehouse-backed apply operation for integration callers.

Uses the Statement Execution API for all Delta state reads/writes --
no ``databricks-connect`` required.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

from genie_space_optimizer.common.warehouse import (
    sql_warehouse_execute,
    sql_warehouse_query,
    wh_load_run,
)

from .config import IntegrationConfig
from .types import ActionResult

logger = logging.getLogger(__name__)

_TERMINAL_APPLY_STATUSES = frozenset({"CONVERGED", "STALLED", "MAX_ITERATIONS"})
_DEFERRED_UC_MODES = frozenset({"uc_artifact", "both"})


def apply_optimization(
    run_id: str,
    ws: WorkspaceClient,
    config: IntegrationConfig,
) -> ActionResult:
    """Legacy apply is disabled; compose the VC/1.0 champion adapter explicitly."""
    raise PermissionError("Use the guarded champion adapter under a target-local Job")


def _get_baseline_and_best(
    iters_rows: list[dict],
) -> tuple[float | None, float | None]:
    """Extract baseline (iteration 0) and best accuracy from iteration rows."""
    baseline: float | None = None
    best: float | None = None
    for row in iters_rows:
        acc = row.get("overall_accuracy")
        if acc is None:
            continue
        try:
            val = float(acc)
        except (TypeError, ValueError):
            continue
        iteration = row.get("iteration")
        try:
            it_num = int(iteration)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if it_num == 0:
            baseline = val
        if best is None or val > best:
            best = val
    return baseline, best
