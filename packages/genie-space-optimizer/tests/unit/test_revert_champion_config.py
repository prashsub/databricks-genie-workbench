"""Unit tests for the integration revert (champion / baseline rollback) path.

Covers the target-selection logic that the Workbench router tests mock out:

* ``target="champion"`` — PATCHes the champion iteration's ``config_json``
  (the row stamped ``is_champion = true``). No baseline fallback: if the
  champion config is unavailable (legacy table / no champion row / unparseable)
  it raises ``ValueError`` so the caller can surface a clear error and the user
  can fall back to the baseline button explicitly.
* ``target="baseline"`` — PATCHes the run's ``config_snapshot`` (the pre-run
  serialized space). Errors when no snapshot was captured.
* guard rails: refuses non-terminal runs, raises on run-not-found.
* a champion config that fails serialized_space validation is promoted to a
  ``RuntimeError`` (router → 422, not 409).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pandas as pd
import pytest

from genie_space_optimizer.integration import revert
from genie_space_optimizer.integration.config import IntegrationConfig


_REAL_ASSERT_NO_ACTIVE_SPACE_RUNS = revert._assert_no_active_space_runs


@pytest.fixture(autouse=True)
def _default_revert_guards(monkeypatch) -> None:
    """Existing target-selection tests run behind successful auth/race guards."""
    monkeypatch.setattr(
        "genie_space_optimizer.common.genie_client.user_can_edit_space",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(revert, "_assert_no_active_space_runs", lambda **kwargs: None)


# ── Fixtures ────────────────────────────────────────────────────────────


def _config() -> IntegrationConfig:
    return IntegrationConfig(
        catalog="main",
        schema_name="gso_test",
        warehouse_id="wh-test",
        job_id=12345,
    )


def _ws() -> MagicMock:
    ws = MagicMock(name="ws")
    # patch_space_config probe (OBO) — keep OBO path so _pick_genie_client
    # returns the user client.
    ws.genie.list_spaces.return_value = MagicMock()
    return ws


def _champion_config() -> dict:
    return {
        "title": "Revenue Space — optimized",
        "serialized_space": {
            "version": 2,
            "data_sources": {"tables": []},
            "instructions": {
                "text_instructions": [{"content": "champion-only hint"}],
            },
        },
    }


def _baseline_config() -> dict:
    return {
        "title": "Revenue Space",
        "serialized_space": {
            "version": 2,
            "data_sources": {"tables": []},
            "instructions": {"text_instructions": []},
        },
    }


def _patch_patch_space_config(monkeypatch, *, live_config: dict | None = None) -> MagicMock:
    patch_mock = MagicMock(name="patch_space_config")
    monkeypatch.setattr(
        "genie_space_optimizer.common.genie_client.patch_space_config",
        patch_mock,
    )
    live = live_config or {
        "_parsed_space": {
            "version": 2,
            "data_sources": {"tables": []},
        },
    }
    monkeypatch.setattr(
        "genie_space_optimizer.common.genie_client.fetch_space_config",
        lambda *a, **k: live,
    )
    return patch_mock


def _live_benchmark(qid: str, question: str, sql: str) -> dict:
    return {
        "id": qid,
        "question": [question],
        "answer": [{"format": "SQL", "content": [sql]}],
    }


# ── target="champion" ───────────────────────────────────────────────────




def test_champion_lookup_disambiguates_duplicate_iteration_zero_rows(monkeypatch) -> None:
    """Recovery may stamp both iteration-0 rows; highest/latest wins."""
    captured_sql: list[str] = []

    def _query(_ws, _warehouse_id, sql):
        captured_sql.append(sql)
        return pd.DataFrame([{"config_json": json.dumps(_champion_config())}])

    monkeypatch.setattr(revert, "sql_warehouse_query", _query)

    result = revert._load_champion_config(
        _ws(), "warehouse-1", "catalog", "schema", "run-duplicate-zero",
    )

    assert result == _champion_config()
    assert "ORDER BY overall_accuracy DESC NULLS LAST" in captured_sql[0]
    assert "timestamp DESC NULLS LAST" in captured_sql[0]


def test_champion_lookup_prefers_api_observed_config(monkeypatch) -> None:
    submitted = _champion_config()
    submitted["serialized_space"]["instructions"]["text_instructions"][0]["content"] = "submitted"
    observed = _champion_config()
    observed["serialized_space"]["instructions"]["text_instructions"][0]["content"] = [
        "observed ",
        "by API",
    ]
    monkeypatch.setattr(
        revert,
        "sql_warehouse_query",
        lambda *_a, **_k: pd.DataFrame([{
            "config_json": json.dumps(submitted),
            "observed_config_json": json.dumps(observed),
        }]),
    )

    result = revert._load_champion_config(
        _ws(), "warehouse-1", "catalog", "schema", "run-observed",
    )

    assert result == observed










def test_preview_revert_options_reports_availability_and_benchmark_diff(
    monkeypatch,
) -> None:
    cfg = _config()
    ws, sp_ws = _ws(), MagicMock(name="sp_ws")
    shared_id = "22222222222222222222222222222222"
    baseline = {
        "version": 2,
        "data_sources": {"tables": []},
        "benchmarks": {"questions": [
            _live_benchmark(
                "11111111111111111111111111111111",
                "baseline only",
                "SELECT 1",
            ),
            _live_benchmark(shared_id, "shared", "SELECT 'baseline'"),
        ]},
    }
    live = {
        "_parsed_space": {
            "version": 2,
            "data_sources": {"tables": []},
            "benchmarks": {"questions": [
                _live_benchmark(shared_id, "shared", "SELECT 'current'"),
                _live_benchmark(
                    "33333333333333333333333333333333",
                    "current only",
                    "SELECT 3",
                ),
            ]},
        },
    }
    run = {
        "run_id": "r-preview",
        "space_id": "space-preview",
        "status": "CONVERGED",
        "config_snapshot": json.dumps({"serialized_space": baseline}),
    }
    monkeypatch.setattr(revert, "wh_load_run", lambda *a, **k: run)
    champion = _champion_config()
    champion["serialized_space"]["benchmarks"] = {"questions": [
        _live_benchmark(
            "44444444444444444444444444444444",
            "champion only",
            "SELECT 4",
        ),
        _live_benchmark(shared_id, "shared", "SELECT 'champion'"),
    ]}
    monkeypatch.setattr(
        revert,
        "sql_warehouse_query",
        lambda *a, **k: pd.DataFrame([{
            "config_json": json.dumps(champion),
        }]),
    )
    _patch_patch_space_config(monkeypatch, live_config=live)

    preview = revert.preview_revert_options("r-preview", ws, sp_ws, cfg)

    assert preview["championAvailable"] is True
    assert preview["baselineAvailable"] is True
    assert preview["benchmarkChampionAvailable"] is True
    assert preview["benchmarkBaselineAvailable"] is True
    assert preview["benchmarkDiffs"] == {
        "champion": {
            "currentCount": 2,
            "targetCount": 2,
            "willAdd": 1,
            "willRemove": 1,
            "willChange": 1,
        },
        "baseline": {
            "currentCount": 2,
            "targetCount": 2,
            "willAdd": 1,
            "willRemove": 1,
            "willChange": 1,
        },
    }






# ── target="baseline" ───────────────────────────────────────────────────








# ── Guard rails ─────────────────────────────────────────────────────────










def test_revert_refuses_when_a_different_run_for_space_is_active(monkeypatch) -> None:
    cfg = _config()
    sp_ws = MagicMock(name="sp_ws")
    runs = pd.DataFrame([
        {"run_id": "history-run", "status": "CONVERGED"},
        {"run_id": "new-run", "status": "IN_PROGRESS"},
    ])
    query = MagicMock(return_value=runs)
    reconcile = MagicMock(return_value=False)
    monkeypatch.setattr(revert, "sql_warehouse_query", query)
    monkeypatch.setattr(revert, "wh_reconcile_active_runs", reconcile)

    with pytest.raises(ValueError, match="new-run"):
        _REAL_ASSERT_NO_ACTIVE_SPACE_RUNS(
            space_id="space-1", sp_ws=sp_ws, config=cfg,
        )

    assert query.call_args.args[0] is sp_ws
    assert "WHERE space_id = 'space-1'" in query.call_args.args[2]
    assert reconcile.call_args.args[0] is sp_ws
    assert reconcile.call_args.args[1] is sp_ws


def test_revert_fails_closed_when_active_run_state_cannot_be_reconciled(monkeypatch) -> None:
    cfg = _config()
    sp_ws = MagicMock(name="sp_ws")
    before = pd.DataFrame([{"run_id": "uncertain-run", "status": "IN_PROGRESS"}])
    after = pd.DataFrame([{
        "run_id": "uncertain-run",
        "status": "FAILED",
        "convergence_reason": "job_run_lookup_failed",
    }])
    query = MagicMock(side_effect=[before, after])
    monkeypatch.setattr(revert, "sql_warehouse_query", query)
    monkeypatch.setattr(revert, "wh_reconcile_active_runs", lambda *args, **kwargs: True)

    with pytest.raises(ValueError, match="STATE_UNVERIFIED"):
        _REAL_ASSERT_NO_ACTIVE_SPACE_RUNS(
            space_id="space-1", sp_ws=sp_ws, config=cfg,
        )
