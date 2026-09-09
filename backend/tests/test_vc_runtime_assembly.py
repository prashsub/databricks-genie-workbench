"""M08: build_vc_runtime wiring.

Offline tests for the injectable runtime assembler and the fail-closed
env/platform shell. The deep per-port construction (Delta facts, gate graph,
identity, SDK clients) is the platform seam `_resolve_platform_ports`, which is
validated during the provisioning/integration phase; offline it must raise
"not integrated" so `main()`/`build_vc_runtime()` stay fail-closed.

Only handlers verified end-to-end are wired this turn (restore ->
RestoreService.run, optimizer_apply -> OptimizerChampionAdapter.apply). Every
other governed kind stays absent from the handler map and therefore fails
closed through GovernedJobRuntime until its handler is wired and
platform-validated.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.jobs import VcJobRuntime, assemble_vc_runtime, build_vc_runtime
from backend.services.version_control.platform.capabilities import (
    FIRST_WRITE_CAPABILITIES,
)
from backend.services.version_control.platform.feature_flags import FeatureFlags

OP = "00000000-0000-4000-8000-000000000011"


def _facts_for(request):
    facts = Mock()
    facts.get_request.return_value.request = request
    return facts


def _restore_request():
    return SimpleNamespace(identity=SimpleNamespace(operation_id=OP),
                           binding=SimpleNamespace(workspace_id="target"),
                           operation_type="restore")


def _ready_probe():
    return lambda: {name: True for name in FIRST_WRITE_CAPABILITIES}


def _all_switches_on():
    return FeatureFlags(vc_writes_enabled=True, vc_restore_enabled=True,
                        vc_promotion_enabled=True, vc_optimizer_apply_enabled=True,
                        vc_reconcile_enabled=True)


def _assemble(**overrides):
    defaults = {
        "workspace_id": "target",
        "facts": _facts_for(_restore_request()),
        "flags": _all_switches_on(),
        "executor": SimpleNamespace(workspace_id="target", principal_id="sp"),
        "identity": Mock(),
        "gate": Mock(),
        "verify_only": Mock(return_value="verified"),
        "attempt_state": Mock(return_value="new"),
        "capability_probe": _ready_probe(),
    }
    defaults.update(overrides)
    return assemble_vc_runtime(**defaults)


def test_restore_handler_runs_service_after_attempt_and_write_gates():
    request = _restore_request()
    restore = Mock()
    restore.run.return_value = "restored"
    executor = SimpleNamespace(workspace_id="target", principal_id="sp")
    runtime = _assemble(facts=_facts_for(request), executor=executor, restore_service=restore)
    assert isinstance(runtime, VcJobRuntime)
    assert runtime.run("restore", OP) == "restored"
    restore.run.assert_called_once_with(OP, executor)


def test_verification_uses_verify_only_not_a_handler():
    verify = Mock(return_value="verified")
    runtime = _assemble(verify_only=verify, restore_service=Mock())
    assert runtime.run("verification", OP) == "verified"
    verify.assert_called_once_with(OP)


def test_unwired_kind_fails_closed():
    runtime = _assemble(restore_service=Mock())
    for kind in ("promotion", "polling", "reconcile", "enrollment", "recovery", "provision"):
        with pytest.raises(PermissionError, match="not integrated"):
            runtime.run(kind, OP)


def test_write_ready_requires_write_switch():
    flags = FeatureFlags(vc_writes_enabled=True, vc_restore_enabled=False)
    runtime = _assemble(flags=flags, restore_service=Mock())
    with pytest.raises(PermissionError, match="disabled write capability"):
        runtime.run("restore", OP)


def test_write_ready_requires_capabilities_and_outage_never_reuses_positive():
    def outage():
        raise TimeoutError("coordination/evidence outage")

    runtime = _assemble(capability_probe=outage, restore_service=Mock())
    with pytest.raises(PermissionError, match="disabled write capability"):
        runtime.run("restore", OP)


def test_optimizer_apply_handler_invokes_champion_adapter():
    champ = SimpleNamespace(identity=SimpleNamespace(operation_id=OP),
                            binding=SimpleNamespace(workspace_id="target"),
                            operation_type="optimizer_apply",
                            optimizer_run_id="run", champion_id="champ", expected_base="base")
    adapter = Mock()
    adapter.apply.return_value = "receipt"
    executor = SimpleNamespace(workspace_id="target", principal_id="sp")
    runtime = _assemble(facts=_facts_for(champ), executor=executor, optimizer_adapter=adapter)
    assert runtime.run("optimizer_apply", OP) == "receipt"
    adapter.apply.assert_called_once_with("run", "champ", champ.binding, "base", executor)


def test_runtime_exposes_ports_for_reconcile_consumers():
    facts, gate, identity = _facts_for(_restore_request()), Mock(), Mock()
    executor = SimpleNamespace(workspace_id="target", principal_id="sp")
    flags = _all_switches_on()
    reconcile = Mock()
    runtime = _assemble(facts=facts, gate=gate, identity=identity, executor=executor,
                        flags=flags, reconcile_service=reconcile)
    assert runtime.facts is facts
    assert runtime.gate is gate
    assert runtime.identity is identity
    assert runtime.executor is executor
    assert runtime.flags is flags
    assert runtime.service is reconcile


def test_unresolved_attempt_state_verifies_without_writing():
    restore = Mock()
    runtime = _assemble(attempt_state=Mock(return_value="unresolved"),
                        verify_only=Mock(return_value="verified"), restore_service=restore)
    assert runtime.run("restore", OP) == "verified"
    restore.run.assert_not_called()


def test_build_vc_runtime_fails_closed_offline():
    for kind in ("restore", "promotion", "optimizer_apply", "reconcile", "verification"):
        with pytest.raises(PermissionError, match="not integrated"):
            build_vc_runtime(kind)


def test_build_vc_runtime_rejects_unknown_kind():
    with pytest.raises(PermissionError, match="not integrated"):
        build_vc_runtime("bogus")
