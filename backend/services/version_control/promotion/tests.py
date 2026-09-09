"""Live bounded preflight test runner for promotion.

`PromotionService.preflight` records `tests.run(rendered, policy, executor)`
evidence and refuses to promote unless both the `validation` and `benchmark`
gates pass. This is the live implementation of that port: it exercises the
*target* warehouse as the executor over the rendered (post-mapping) identifiers,
so a promotion is blocked when a mapped table is unresolvable or empty.

Determinism is a hard requirement: `preflight` runs once when the operation is
approved and again inside the promotion Job, and the two `preflight_evidence_digest`
values must match. The recorded evidence therefore captures only the stable
inputs (which identifiers were checked against which threshold and the pass/fail
outcome) — never volatile row counts — even though the live COUNT probe runs on
every call. The credentialed SQL execution lives in the injected `query` seam so
the decision logic stays offline-testable.
"""

from collections.abc import Callable
from typing import Any

from .mapping import RenderedPayload, structured_identifiers


def _qualify(identifier: str) -> str:
    return ".".join(f"`{part}`" for part in identifier.split("."))


class PreflightTestRunner:
    def __init__(self, *, query: Callable[[str, Any], list],
                 identifiers: Callable[[dict], Any] = structured_identifiers):
        self._query = query
        self._identifiers = identifiers

    def _tables(self, rendered: RenderedPayload) -> list[str]:
        return sorted({entry["identifier"] for entry, kind in self._identifiers(rendered.serialized_space)
                       if kind in ("table", "metric_view")})

    def run(self, rendered: RenderedPayload, policy, executor) -> dict:
        tables = self._tables(rendered)
        thresholds = policy.thresholds or {}
        return {
            "validation": self._validation(tables, thresholds.get("validation", {}) or {}, executor),
            "benchmark": self._benchmark(tables, thresholds.get("benchmark", {}) or {}, executor),
        }

    def _validation(self, tables: list[str], threshold: dict, executor) -> dict:
        if threshold.get("smoke") is not True:
            return {"passed": True, "smoke": False, "tables": tables}
        for table in tables:
            try:
                self._query(f"SELECT * FROM {_qualify(table)} LIMIT 0", executor)
            except Exception as error:  # noqa: BLE001 - any resolution failure is a smoke failure
                return {"passed": False, "smoke": True, "tables": tables,
                        "failed": table, "reason": type(error).__name__}
        return {"passed": True, "smoke": True, "tables": tables}

    def _benchmark(self, tables: list[str], threshold: dict, executor) -> dict:
        minimum = threshold.get("minimum")
        if minimum is None:
            return {"passed": True, "minimum": None, "tables": tables}
        for table in tables:
            rows = self._query(f"SELECT COUNT(*) FROM {_qualify(table)}", executor)
            count = int(rows[0][0]) if rows and rows[0] else 0
            if count < minimum:
                return {"passed": False, "minimum": minimum, "tables": tables, "failed": table}
        return {"passed": True, "minimum": minimum, "tables": tables}
