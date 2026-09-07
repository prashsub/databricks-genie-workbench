from unittest.mock import Mock

from genie_space_optimizer.integration.version_control import ChampionRun


def test_unapplied_and_abandoned_runs_create_no_version():
    adapter = Mock()
    run = ChampionRun("run", adapter)
    for i in range(3):
        run.record_iteration(str(i), {"candidate": i}, float(i))
    assert len(run.iterations) == 3
    adapter.apply.assert_not_called()
    run.abandon()
    assert run.abandoned
    assert run.receipt is None
    adapter.apply.assert_not_called()
