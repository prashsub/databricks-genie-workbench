from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from genie_space_optimizer.integration.version_control import ChampionRun
from genie_space_optimizer.integration import revert


@pytest.mark.parametrize("status", ["applied_partial", "applied_unverified", "quarantined"])
def test_partial_or_ambiguous_apply_never_retries_or_reverts_in_process(status):
    adapter = Mock()
    adapter.apply.return_value = SimpleNamespace(status=status, unresolved=True,
                                                preimage=None, postimage=None,
                                                evidence_references=("checkpoint",), operation_id="operation")
    run = ChampionRun("run", adapter)
    receipt = run.apply("champion", "binding", "base", "job")
    assert receipt.unresolved
    assert run.recovery_handle.operation_id == "operation"
    assert run.recovery_handle.job_kind == "recovery"
    assert run.apply("champion", "binding", "base", "job") is receipt
    assert run.post_version is None
    adapter.apply.assert_called_once()
    adapter.revert.assert_not_called()


def test_unknown_adapter_exception_latches_against_in_process_retry():
    adapter = Mock()
    adapter.apply.side_effect = TimeoutError("response lost")
    run = ChampionRun("run", adapter)
    with pytest.raises(TimeoutError):
        run.apply("champion", "binding", "base", "job")
    with pytest.raises(RuntimeError, match="recovery"):
        run.apply("champion", "binding", "base", "job")
    adapter.apply.assert_called_once()


@pytest.mark.parametrize("target", ["champion", "baseline"])
def test_legacy_revert_is_disabled_before_any_platform_access(target):
    client = Mock()
    with pytest.raises(PermissionError, match="governed restore Job"):
        revert.revert_optimization("run", client, client, Mock(), target=target)
    assert client.mock_calls == []


def test_retired_revert_helper_cannot_patch_or_compensate():
    client = Mock()
    with pytest.raises(PermissionError, match="governed restore Job"):
        revert._apply_revert_state(client=client, space_id="live", target="baseline",
                                   run_id="run", target_config={}, target_description="new",
                                   live_config={}, live_description="old")
    assert client.mock_calls == []


def test_historical_revert_dispatches_governed_restore_job():
    from genie_space_optimizer.integration.version_control import RestoreSelection

    selection = RestoreSelection("run", "historical-version", "binding", "base", "approval")
    jobs = Mock()
    jobs.submit.return_value = SimpleNamespace(operation_id="restore-operation", job_run_id="job/restore")
    client = Mock()
    result = revert.revert_optimization("run", client, client, Mock(), selection=selection,
                                        restore_jobs=jobs, requester="human", writes_enabled=True)
    assert result is jobs.submit.return_value
    jobs.submit.assert_called_once_with(selection, "human")
    assert client.mock_calls == []
    with pytest.raises(PermissionError):
        revert.revert_optimization("run", client, client, Mock(), selection=selection,
                                   restore_jobs=jobs, requester="human")
    jobs.submit.assert_called_once()


def test_discard_only_updates_telemetry_and_never_rolls_back_managed_state(monkeypatch):
    from genie_space_optimizer.integration import discard

    monkeypatch.setattr(discard, "wh_load_run", lambda *args: {
        "status": "CONVERGED", "space_id": "live", "config_snapshot": {"version": 2},
    })
    monkeypatch.setattr("genie_space_optimizer.common.genie_client.user_can_edit_space", lambda *args, **kwargs: True)
    monkeypatch.setattr(discard, "_assert_no_active_space_runs", lambda **kwargs: None)
    write = Mock()
    monkeypatch.setattr(discard, "sql_warehouse_execute", write)
    rollback = Mock(return_value={"status": "SUCCESS"})
    monkeypatch.setattr("genie_space_optimizer.optimization.applier.rollback", rollback)
    result = discard.discard_optimization("run", Mock(), Mock(), Mock())
    assert result.status == "discarded"
    rollback.assert_not_called()
    write.assert_called_once()


def test_legacy_apply_cannot_claim_applied_without_gate():
    from genie_space_optimizer.integration.apply import apply_optimization

    client = Mock()
    with pytest.raises(PermissionError, match="champion"):
        apply_optimization("run", client, Mock())
    assert client.mock_calls == []
