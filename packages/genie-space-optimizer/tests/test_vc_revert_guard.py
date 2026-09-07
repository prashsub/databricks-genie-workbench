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
