from unittest.mock import Mock
from types import SimpleNamespace

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


def test_champion_version_uses_authoritative_readback_not_candidate_payload():
    adapter = Mock()
    observed = SimpleNamespace(version_id="observed", binding_id="binding", binding_revision=1)
    adapter.apply.return_value = SimpleNamespace(status="confirmed", postimage=observed,
                                                preimage=None, unresolved=False,
                                                evidence_references=(), operation_id="operation")
    run = ChampionRun("run", adapter)
    candidate = {"instructions": ["unnormalized"]}
    run.record_iteration("champion", candidate, 90)
    receipt = run.apply("champion", "binding", "base", "job")
    assert receipt is adapter.apply.return_value
    assert run.post_version is observed
    assert run.post_version is not candidate
    assert run.iterations[0][1] == candidate
    adapter.apply.assert_called_once_with("run", "champion", "binding", "base", "job")


def test_external_preimage_at_champion_apply_is_preserved_without_iteration_version():
    external = SimpleNamespace(version_id="external", origin="external")
    champion = SimpleNamespace(version_id="final", origin="optimizer")
    adapter = Mock()
    adapter.apply.return_value = SimpleNamespace(status="confirmed", preimage=external,
                                                postimage=champion, unresolved=False,
                                                evidence_references=("external", "checkpoint", "final"))
    run = ChampionRun("run", adapter)
    for iteration in range(5):
        run.record_iteration(str(iteration), {"iteration": iteration}, iteration)
    run.apply("4", "binding", "base", "job")
    assert run.pre_version is external
    assert run.evidence_references == ("external", "checkpoint", "final")
    assert [version.origin for version in (run.pre_version, run.post_version)].count("optimizer") == 1
    assert len(run.iterations) == 5
    adapter.apply.assert_called_once()


def test_noop_champion_has_receipt_but_no_new_optimizer_version():
    adapter = Mock()
    existing = SimpleNamespace(version_id="existing")
    adapter.apply.return_value = SimpleNamespace(status="noop", preimage=existing,
                                                postimage=existing, unresolved=False,
                                                evidence_references=("existing",), operation_id="noop-operation")
    run = ChampionRun("run", adapter)
    receipt = run.apply("champion", "binding", "base", "job")
    assert receipt.operation_id == "noop-operation"
    assert run.receipt is receipt
    assert run.pre_version is existing
    assert run.post_version is None
