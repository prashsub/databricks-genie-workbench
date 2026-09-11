"""Fail-closed app wiring for the observe surface: config, vc_auth, mounting."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.version_control import contracts as vc
from backend.services.version_control.platform.app_observe import (
    FailClosedRestore,
    mount_observe_routers,
    observe_config_from_env,
    vc_auth_from_request,
)
from backend.services.version_control.platform.observe_seams import build_observe_runtime
from backend.tests.test_vc_live_seams import FakeAdapters, TARGET_HOST

_FULL_ENV = {
    "VC_OBSERVE_WORKSPACE_ID": "target",
    "VC_OBSERVE_CATALOG": "sandbox_cat",
    "VC_OBSERVE_CONTROL_SCHEMA": "vc_ctl",
    "VC_OBSERVE_WAREHOUSE_ID": "wh-123",
    "VC_OBSERVE_HOST": TARGET_HOST,
    "VC_OBSERVE_SP_PRINCIPAL_ID": "target-sp",
    "VC_HISTORY_ENABLED": "true",
}


@pytest.mark.parametrize("drop", list(_FULL_ENV.keys() - {"VC_HISTORY_ENABLED"}))
def test_config_is_none_when_a_required_var_is_missing(drop):
    env = {k: v for k, v in _FULL_ENV.items() if k != drop}
    assert observe_config_from_env(env) is None


def test_config_builds_full_shape_from_env():
    config = observe_config_from_env(_FULL_ENV)
    assert config["workspace_id"] == "target"
    assert config["catalog"] == "sandbox_cat" and config["control_schema"] == "vc_ctl"
    assert config["target_selection"]["principal_id"] == "target-sp"
    assert config["target_selection"]["execution_ref"] == "app/genie-workbench"
    assert config["roles"]["executor"]["warehouse_id"] == "wh-123"
    assert config["flags"] == {"vc_history_enabled": True, "vc_writes_enabled": False,
                               "vc_restore_enabled": False}


def test_config_empty_env_is_none():
    assert observe_config_from_env({}) is None


def test_vc_auth_prefers_forwarded_user_then_email_then_dev():
    assert vc_auth_from_request({"X-Forwarded-User": "a@x"}, "ws") == vc.AuthenticatedRequest("a@x", "ws")
    assert vc_auth_from_request({"X-Forwarded-Email": "b@x"}, "ws") == vc.AuthenticatedRequest("b@x", "ws")
    assert vc_auth_from_request({}, "ws", dev_email="d@x") == vc.AuthenticatedRequest("d@x", "ws")
    assert vc_auth_from_request({}, "ws") is None


def test_fail_closed_restore_never_executes():
    with pytest.raises(PermissionError, match="not wired"):
        FailClosedRestore().submit("op-1", object())


def _app_with_observe(*, authenticated: bool):
    runtime = build_observe_runtime(observe_config_from_env(_FULL_ENV), adapters=FakeAdapters())
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        if authenticated:
            request.state.vc_auth = vc.AuthenticatedRequest("user@x", runtime.workspace_id)
        return await call_next(request)

    mount_observe_routers(app, runtime)
    return app, runtime


def test_mount_exposes_history_and_observe_and_restore_routes():
    app, _ = _app_with_observe(authenticated=True)
    paths = {(route.path, tuple(sorted(route.methods))) for route in app.routes
             if getattr(route, "path", "").startswith("/api/version-control")}
    assert ("/api/version-control/bindings/{binding_id}/versions", ("GET",)) in paths
    assert ("/api/version-control/bindings/{binding_id}/observe", ("POST",)) in paths
    assert ("/api/version-control/bindings/{binding_id}/restore", ("POST",)) in paths


def test_history_requires_vc_auth_when_mounted():
    app, _ = _app_with_observe(authenticated=False)
    binding_id = "00000000-0000-4000-8000-000000000001"
    response = TestClient(app).get(f"/api/version-control/bindings/{binding_id}/versions")
    assert response.status_code == 401
