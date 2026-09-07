import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services.version_control import platform


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
