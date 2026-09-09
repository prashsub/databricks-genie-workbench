"""Offline tests for the live promotion preflight test runner."""

from backend.services.version_control import contracts as vc
from backend.services.version_control.promotion.mapping import RenderedPayload
from backend.services.version_control.promotion.packages import digest
from backend.services.version_control.promotion.tests import PreflightTestRunner

_EXEC = object()
_RENDERED = RenderedPayload(
    {"version": 2, "data_sources": {"tables": [{"identifier": "prod.sales.orders"}]}},
    "Sales", {"warehouse_id": "wh", "parent_path": "/p", "consumers": ()})
_POLICY = vc.TestPolicy("VC/1.0", "0" * 64, "0" * 64,
                        {"validation": {"smoke": True}, "benchmark": {"minimum": 1}})


def _runner(rows_by_sql):
    seen = []

    def query(statement, executor):
        seen.append(statement)
        for fragment, rows in rows_by_sql.items():
            if fragment in statement:
                return rows
        return []

    return PreflightTestRunner(query=query), seen


def test_validation_and_benchmark_pass_for_populated_table():
    runner, seen = _runner({"COUNT(*)": [[5]]})
    result = runner.run(_RENDERED, _POLICY, _EXEC)
    assert result["validation"]["passed"] is True
    assert result["benchmark"]["passed"] is True
    # Genuinely exercised the warehouse: a bounded smoke read + a count.
    assert any("LIMIT 0" in s for s in seen) and any("COUNT(*)" in s for s in seen)
    assert all("`prod`.`sales`.`orders`" in s for s in seen)


def test_benchmark_fails_below_minimum():
    runner, _ = _runner({"COUNT(*)": [[0]]})
    result = runner.run(_RENDERED, _POLICY, _EXEC)
    assert result["validation"]["passed"] is True
    assert result["benchmark"]["passed"] is False


def test_validation_fails_when_smoke_query_raises():
    def query(statement, executor):
        if "LIMIT 0" in statement:
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
        return [[1]]

    runner = PreflightTestRunner(query=query)
    result = runner.run(_RENDERED, _POLICY, _EXEC)
    assert result["validation"]["passed"] is False
    assert result["benchmark"]["passed"] is True


def test_evidence_is_deterministic_across_runs():
    # The preflight runs at approval time and again at execute time; the two
    # evidence digests must match, so volatile data (row counts) must never be
    # baked into the recorded evidence.
    runner_a, _ = _runner({"COUNT(*)": [[5]]})
    runner_b, _ = _runner({"COUNT(*)": [[999]]})
    a = runner_a.run(_RENDERED, _POLICY, _EXEC)
    b = runner_b.run(_RENDERED, _POLICY, _EXEC)
    assert digest(a["validation"]) == digest(b["validation"])
    assert digest(a["benchmark"]) == digest(b["benchmark"])


def test_no_thresholds_pass_trivially():
    runner, seen = _runner({})
    policy = vc.TestPolicy("VC/1.0", "0" * 64, "0" * 64, {})
    result = runner.run(_RENDERED, policy, _EXEC)
    assert result["validation"]["passed"] is True
    assert result["benchmark"]["passed"] is True
    assert seen == []
