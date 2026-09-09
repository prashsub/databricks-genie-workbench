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
from backend.services.version_control.platform.jobs import _load_governed_config, main

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
