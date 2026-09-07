import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services.version_control import platform
from backend.services.version_control.contracts import ExplicitExecutorSelection


def test_wrong_run_as_fails_startup_without_self_healing():
    verify = getattr(platform, "verify_configured_job_run_as", None)
    assert callable(verify), "Startup must verify Job identity, never repair it"
    client = Mock()
    client.config.client_id = "app-sp"
    job = SimpleNamespace(settings=SimpleNamespace(
        run_as=SimpleNamespace(service_principal_name="wrong-sp", user_name=None)))
    client.jobs.get.return_value = job
    factory = Mock(return_value=client)
    with pytest.raises(PermissionError, match="run_as"):
        verify({"GSO_JOB_ID": "42"}, factory)
    client.jobs.update.assert_not_called()
    client.jobs.reset.assert_not_called()
    job.settings.run_as.service_principal_name = "app-sp"
    verify({"GSO_JOB_ID": "42"}, factory)
    client.jobs.get.side_effect = TimeoutError("identity unavailable")
    with pytest.raises(TimeoutError):
        verify({"GSO_JOB_ID": "42"}, factory)
    with pytest.raises(ValueError):
        verify({"GSO_JOB_ID": "not-a-job"}, factory)
    factory.reset_mock()
    verify({}, factory)
    factory.assert_not_called()
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "ensure_job_run_as" not in source
    tree = ast.parse(source)
    startup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "startup")
    assert isinstance(startup.body[0], ast.Expr)
    assert startup.body[0].value.func.id == "_verify_gso_job_run_as"


def test_explicit_profile_host_workspace_mismatch_is_rejected():
    provider_type = getattr(platform, "PlatformIdentityProvider", None)
    assert callable(provider_type), "VC identity requires explicit credential selection"
    client = Mock()
    client.config.host = "https://target.example"
    client.config.auth_type = "oauth-m2m"
    client.get_workspace_id.return_value = "target"
    client.current_user.me.return_value = SimpleNamespace(application_id="target-sp", id="scim-id")
    factory = Mock(return_value=client)
    provider = provider_type(profiles={"target-test": factory})
    selection = ExplicitExecutorSelection("target", "https://target.example", "target-sp", "42", "target-test")
    context = provider.executor(selection)
    assert context.principal_id == "target-sp"
    assert context.credential_handle is client
    assert "credential_handle" not in repr(context)
    assert provider.sql_client(context) is client.statement_execution
    assert provider.volume_client(context) is client.files
    for field, value in (("host", "https://other.example"), ("workspace_id", "source"),
                         ("principal_id", "other-sp"), ("profile", "DEFAULT"), ("profile", None),
                         ("host", "https://target.example/path"), ("host", "http://target.example")):
        from dataclasses import replace
        with pytest.raises((ValueError, PermissionError)):
            provider.executor(replace(selection, **{field: value}))
    client.config.auth_type = "pat"
    with pytest.raises(PermissionError):
        provider.executor(selection)
    factory.reset_mock()
    provider = provider_type()
    with pytest.raises(PermissionError):
        provider.executor(selection)
    factory.assert_not_called()


def test_production_viewer_capture_uses_trusted_reader_without_elevation():
    capture_type = getattr(platform, "TrustedSnapshotReader", None)
    assert callable(capture_type), "Viewer policy and full-config reader must remain separate"
    from backend.services.version_control.contracts import ActorContext, AuthenticatedRequest
    actor = ActorContext("viewer", "target", "user")
    resolve_actor = Mock(return_value=actor)
    groups = Mock(return_value=frozenset({"history-readers"}))
    edit = Mock(return_value=False)
    provider = platform.PlatformIdentityProvider(request_resolver=resolve_actor,
                                                 group_resolver=groups, edit_resolver=edit)
    request = AuthenticatedRequest("server-session", "target")
    binding = SimpleNamespace(workspace_id="target", space_id="space")
    assert provider.actor(request) == actor
    assert provider.groups("viewer", "target") == frozenset({"history-readers"})
    assert provider.can_edit("viewer", binding) is False
    authorizer = Mock(return_value=False)
    full_reader = Mock(return_value={"serialized_space": "{}"})
    capture = capture_type(provider, authorizer, {"target": full_reader})
    with pytest.raises(PermissionError):
        capture.read(request, binding)
    full_reader.assert_not_called()
    authorizer.return_value = True
    assert capture.read(request, binding) == {"serialized_space": "{}"}
    authorizer.assert_called_with(actor, binding)
    full_reader.assert_called_once_with(binding)
    assert provider.can_edit("viewer", binding) is False
    full_reader.reset_mock()
    with pytest.raises(PermissionError):
        capture.read(request, SimpleNamespace(workspace_id="source", space_id="space"))
    full_reader.assert_not_called()
    authorizer.side_effect = TimeoutError("policy unavailable")
    with pytest.raises(TimeoutError):
        capture.read(request, binding)
    full_reader.assert_not_called()
    with pytest.raises(PermissionError):
        platform.PlatformIdentityProvider().actor(request)


def test_obo_executor_is_request_bound_and_never_falls_back():
    client = Mock()
    client.config.host = "https://target.example"
    client.config.auth_type = "pat"
    client.get_workspace_id.return_value = "target"
    client.current_user.me.return_value = SimpleNamespace(application_id=None, id="viewer")
    provider = platform.PlatformIdentityProvider(obo_executors={"session": lambda: client})
    selection = ExplicitExecutorSelection("target", "https://target.example", "viewer", "session", None)
    assert provider.executor(selection).actor_kind == "user"
    from dataclasses import replace
    with pytest.raises(PermissionError):
        provider.executor(replace(selection, execution_ref="other-session"))
    client.current_user.me.side_effect = PermissionError("expired user token")
    with pytest.raises(PermissionError):
        provider.executor(selection)
