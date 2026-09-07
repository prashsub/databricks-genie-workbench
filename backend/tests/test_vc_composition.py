from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace

import pytest
import yaml

from backend.services.version_control import contracts, platform
from backend.services.version_control.platform.feature_flags import FeatureFlags, WRITE_SWITCHES


def test_modules_are_wired_once_with_write_switch_default_off():
    compose = getattr(platform, "compose", None)
    assert callable(compose), "Root must install a lazy, explicitly injected VC composition"
    factory = Mock(return_value=object())
    container = compose({contracts.IdentityProvider: factory})
    factory.assert_not_called()
    assert container.resolve(contracts.IdentityProvider) is container.resolve(contracts.IdentityProvider)
    factory.assert_called_once_with()
    with pytest.raises(ValueError, match="already registered"):
        container.register(contracts.IdentityProvider, factory)
    assert all(not container.flags.enabled(name) for name in WRITE_SWITCHES)
    assert not FeatureFlags(vc_restore_enabled=True).enabled("vc_restore_enabled")
    with pytest.raises(KeyError):
        container.resolve(contracts.Coordination)
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "app.state.version_control = compose()" in source


def test_jobs_take_operation_id_and_retry_verification_not_mutation():
    dispatcher_type = getattr(platform, "LocalJobDispatcher", None)
    assert callable(dispatcher_type), "Dispatch must load durable target-local requests"
    operation_id = "00000000-0000-4000-8000-000000000011"
    client = Mock()
    client.jobs.run_now.return_value.run_id = 123
    identity = Mock()
    identity.verified_client.return_value = client
    executor = SimpleNamespace(workspace_id="target", principal_id="target-sp")
    request = SimpleNamespace(identity=SimpleNamespace(operation_id=operation_id),
                              binding=SimpleNamespace(workspace_id="target"), operation_type="restore")
    facts = Mock()
    facts.get_request.return_value.request = request
    ready = Mock(return_value=False)
    dispatcher = dispatcher_type(identity, executor, facts, {"restore": 42}, ready)
    with pytest.raises(PermissionError):
        dispatcher.submit_local(operation_id, "restore")
    client.jobs.run_now.assert_not_called()
    ready.return_value = True
    handle = dispatcher.submit_local(operation_id, "restore")
    assert handle.operation_id == operation_id and handle.job_run_id == "123"
    submitted = client.jobs.run_now.call_args.kwargs
    assert submitted["job_parameters"] == {"operation_id": operation_id}
    assert len(submitted["idempotency_token"]) == 64
    dispatcher.submit_local(operation_id, "restore")
    assert client.jobs.run_now.call_args.kwargs == submitted
    request.binding.workspace_id = "source"
    with pytest.raises(PermissionError):
        dispatcher.submit_local(operation_id, "restore")
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "resources/version-control/platform.jobs.yml").read_text())
    jobs = config["targets"]["vc-sandbox"]["resources"]["jobs"]
    kinds = {"restore", "reconcile", "promotion", "polling", "optimizer_apply", "enrollment", "recovery", "verification", "provision"}
    assert set(jobs) == {f"vc-{kind}" for kind in kinds}
    for kind in kinds:
        job = jobs[f"vc-{kind}"]
        assert job["max_concurrent_runs"] == 1
        assert job["queue"]["enabled"] is True
        assert job["run_as"]["service_principal_name"].startswith("${var.vc_")
        assert job["parameters"] == [{"name": "operation_id", "default": ""}]
        task = job["tasks"][0]
        assert task["max_retries"] == (2 if kind == "verification" else 0)
        assert task["retry_on_timeout"] is False
        assert task["python_wheel_task"]["parameters"] == ["--kind", kind, "--operation-id", "{{job.parameters.operation_id}}"]


def test_job_runtime_refuses_unintegrated_handlers_and_verifies_retries():
    runtime_type = getattr(platform, "GovernedJobRuntime", None)
    assert callable(runtime_type)
    operation_id = "00000000-0000-4000-8000-000000000011"
    request = SimpleNamespace(identity=SimpleNamespace(operation_id=operation_id),
                              binding=SimpleNamespace(workspace_id="target"), operation_type="restore")
    facts = Mock()
    facts.get_request.return_value.request = request
    execute = Mock()
    verify = Mock(return_value="verified")
    state = Mock(return_value="unresolved")
    runtime = runtime_type(facts, "target", {"restore": execute}, verify, state, lambda kind: True)
    assert runtime.run("restore", operation_id) == "verified"
    execute.assert_not_called()
    state.return_value = "new"
    runtime.run("restore", operation_id)
    execute.assert_called_once_with(request)
    with pytest.raises(PermissionError):
        runtime.run("unknown", operation_id)
    from backend.services.version_control.platform.jobs import main
    with pytest.raises(PermissionError, match="not integrated"):
        main(["--kind", "restore", "--operation-id", operation_id])
