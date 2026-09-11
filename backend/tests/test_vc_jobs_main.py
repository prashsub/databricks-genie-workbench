"""`platform.jobs.main` governed-config loading (D2.4f).

The governed write kinds (promotion) load a large reviewed target config from an
immutable JSON document (a UC Volume URI in the Job, a local path offline) via
`--governed-config`, then hand it to `build_vc_runtime`. Absence leaves the kind
fail-closed. Provisioning keeps its inline CLI config; verification/other kinds
without a config URI stay fail-closed.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import backend.jobs as jobs_pkg
from backend.services.version_control.platform.jobs import (
    _bootstrap_role_secrets,
    _load_governed_config,
    main,
)

OP = "00000000-0000-4000-8000-000000000011"


def test_load_governed_config_none_returns_none():
    assert _load_governed_config(None) is None
    assert _load_governed_config("") is None


def test_load_governed_config_reads_local_json(tmp_path):
    doc = {"workspace_id": "target", "volumes": {"outbound": "/Volumes/c/s/o"}}
    path = tmp_path / "governed.json"
    path.write_text(json.dumps(doc))
    assert _load_governed_config(str(path)) == doc


def test_main_promotion_loads_config_and_dispatches(tmp_path, monkeypatch):
    doc = {"workspace_id": "target", "catalog": "c", "control_schema": "s",
           "warehouse_id": "wh", "principals": {"runtime": "sp"}}
    path = tmp_path / "governed.json"
    path.write_text(json.dumps(doc))

    runtime = Mock()
    runtime.run.return_value = "receipt"
    build = Mock(return_value=runtime)
    monkeypatch.setattr(jobs_pkg, "build_vc_runtime", build)

    result = main(["--kind", "promotion", "--operation-id", OP,
                   "--governed-config", str(path)])
    assert result == "receipt"
    build.assert_called_once_with("promotion", doc)
    runtime.run.assert_called_once_with("promotion", OP)


def test_main_promotion_without_config_stays_fail_closed():
    # No --governed-config -> config is None -> the real build_vc_runtime routes
    # through the fail-closed governed seams and refuses.
    with pytest.raises(PermissionError, match="not integrated"):
        main(["--kind", "promotion", "--operation-id", OP])


def test_main_promotion_reads_volume_uri_as_job_identity(monkeypatch):
    doc = {"workspace_id": "target"}
    downloaded = SimpleNamespace(contents=SimpleNamespace(
        read=lambda: json.dumps(doc).encode()))
    client = Mock()
    client.files.download.return_value = downloaded

    from databricks import sdk
    monkeypatch.setattr(sdk, "WorkspaceClient", Mock(return_value=client))

    uri = "/Volumes/cat/ctl/vc_config/governed.json"
    assert _load_governed_config(uri) == doc
    client.files.download.assert_called_once_with(uri)


def _b64(value):
    import base64

    return base64.b64encode(value.encode()).decode()


def test_bootstrap_reads_scope_and_sets_env(monkeypatch):
    # The Job self-reads each M2M credential from the scope via the ambient run_as
    # (executor, granted READ) and populates os.environ before the runtime builds.
    for name in ("VC_EXECUTOR_CLIENT_ID", "VC_EXECUTOR_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    values = {"executor_client_id": "cid", "executor_client_secret": "csec"}
    client = Mock()
    client.api_client.do.side_effect = lambda method, path, query: {
        "value": _b64(values[query["key"]])}
    from databricks import sdk
    monkeypatch.setattr(sdk, "WorkspaceClient", Mock(return_value=client))

    _bootstrap_role_secrets({"scope": "vc-promotion", "env": {
        "VC_EXECUTOR_CLIENT_ID": "executor_client_id",
        "VC_EXECUTOR_CLIENT_SECRET": "executor_client_secret"}})

    import os
    assert os.environ["VC_EXECUTOR_CLIENT_ID"] == "cid"
    assert os.environ["VC_EXECUTOR_CLIENT_SECRET"] == "csec"


def test_bootstrap_never_overwrites_existing_env(monkeypatch):
    # Offline/local runs inject the credentials directly; the scope read is skipped.
    monkeypatch.setenv("VC_EXECUTOR_CLIENT_ID", "local-cid")
    client = Mock()
    from databricks import sdk
    monkeypatch.setattr(sdk, "WorkspaceClient", Mock(return_value=client))

    _bootstrap_role_secrets({"scope": "vc-promotion", "env": {
        "VC_EXECUTOR_CLIENT_ID": "executor_client_id"}})

    import os
    assert os.environ["VC_EXECUTOR_CLIENT_ID"] == "local-cid"
    client.api_client.do.assert_not_called()


def test_bootstrap_fails_closed_on_missing_secret(monkeypatch):
    monkeypatch.delenv("VC_SOURCE_CLIENT_ID", raising=False)
    client = Mock()
    client.api_client.do.return_value = {"value": ""}
    from databricks import sdk
    monkeypatch.setattr(sdk, "WorkspaceClient", Mock(return_value=client))

    with pytest.raises(PermissionError, match="unavailable"):
        _bootstrap_role_secrets({"scope": "vc-promotion", "env": {
            "VC_SOURCE_CLIENT_ID": "source_client_id"}})


def test_main_promotion_bootstraps_secrets_before_build(tmp_path, monkeypatch):
    monkeypatch.delenv("VC_EXECUTOR_CLIENT_ID", raising=False)
    doc = {"workspace_id": "target", "secret_bootstrap": {
        "scope": "vc-promotion", "env": {"VC_EXECUTOR_CLIENT_ID": "executor_client_id"}}}
    path = tmp_path / "governed.json"
    path.write_text(json.dumps(doc))

    client = Mock()
    client.api_client.do.return_value = {"value": _b64("cid")}
    from databricks import sdk
    monkeypatch.setattr(sdk, "WorkspaceClient", Mock(return_value=client))

    seen = {}

    def _build(kind, config):
        import os
        seen["env"] = os.environ.get("VC_EXECUTOR_CLIENT_ID")
        runtime = Mock()
        runtime.run.return_value = "receipt"
        return runtime

    monkeypatch.setattr(jobs_pkg, "build_vc_runtime", _build)

    assert main(["--kind", "promotion", "--operation-id", OP,
                 "--governed-config", str(path)]) == "receipt"
    # Credentials are present in the environment by the time the runtime is built.
    assert seen["env"] == "cid"
