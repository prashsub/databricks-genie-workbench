"""Space-keyed VC bridge router: history reads + auto-enroll capture for a Genie space."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.version_control import contracts as vc

SPACE_ID = "space-1"
_BINDING = vc.BindingRef(str(UUID(int=5)), 1, SPACE_ID, "target", SPACE_ID, "prod")


def _summary():
    fingerprints = vc.Fingerprints("a" * 64, "b" * 64, "c" * 64, "vc-c14n/1")
    return vc.VersionSummary(str(UUID(int=9)), _BINDING.binding_id, datetime.now(timezone.utc),
                             vc.Origin.EXTERNAL, "user@x", None, None, fingerprints, "run-1", None)


def _status():
    return vc.BindingStatus(_BINDING.binding_id, 1, vc.Heads(str(UUID(int=9)), None, None),
                            vc.DriftState.CLEAN, False, None, datetime.now(timezone.utc),
                            datetime.now(timezone.utc), False, (), ())


def _runtime(*, history=True, writes=True, existing=_BINDING):
    ledger = Mock()
    ledger.history.return_value = vc.VersionPage((_summary(),), "cursor-2")
    registry = Mock()
    registry.find_active_by_space_key.return_value = existing
    observer = Mock()
    observer.capture_on_open.return_value = vc.ObservationResult(_status(), _summary(), False)
    actor = vc.ActorContext("user@x", "target", "human")
    identity = Mock()
    identity.actor.return_value = actor
    flags = Mock()
    flags.enabled.side_effect = lambda switch: {
        "vc_history_enabled": history, "vc_writes_enabled": writes}.get(switch, False)
    return SimpleNamespace(
        ledger=ledger, registry=registry, observer=observer, identity=identity, flags=flags,
        authorize_history=Mock(return_value=True), actor=actor, workspace_id="target",
        environment="prod", reader_selection=object())


def _client(runtime, *, authenticated=True):
    from backend.routers.vc_spaces import build_router

    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        if authenticated:
            request.state.vc_auth = vc.AuthenticatedRequest("user@x", "target")
        return await call_next(request)

    app.include_router(build_router(runtime=runtime))
    return TestClient(app)


def test_space_versions_returns_history_when_enrolled():
    runtime = _runtime()
    response = _client(runtime).get(f"/api/version-control/spaces/{SPACE_ID}/versions?limit=5")
    assert response.status_code == 200
    assert response.json()["items"][0]["optimizer_run_id"] == "run-1"
    runtime.ledger.history.assert_called_once_with(_BINDING, None, 5)


def test_space_versions_is_empty_when_not_enrolled():
    runtime = _runtime(existing=None)
    response = _client(runtime).get(f"/api/version-control/spaces/{SPACE_ID}/versions")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    runtime.ledger.history.assert_not_called()


def test_space_versions_fail_closed_when_history_disabled():
    runtime = _runtime(history=False)
    response = _client(runtime).get(f"/api/version-control/spaces/{SPACE_ID}/versions")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "vc_history_disabled"


def test_space_versions_requires_authentication():
    runtime = _runtime()
    response = _client(runtime, authenticated=False).get(
        f"/api/version-control/spaces/{SPACE_ID}/versions")
    assert response.status_code == 401


def test_space_observe_captures_current_state():
    runtime = _runtime()
    response = _client(runtime).post(f"/api/version-control/spaces/{SPACE_ID}/observe")
    assert response.status_code == 200
    assert response.json()["captured_version"]["version_id"] == str(UUID(int=9))
    runtime.observer.capture_on_open.assert_called_once_with(_BINDING, runtime.actor)


def test_space_observe_fail_closed_when_writes_disabled():
    runtime = _runtime(writes=False)
    response = _client(runtime).post(f"/api/version-control/spaces/{SPACE_ID}/observe")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "vc_writes_disabled"
    runtime.observer.capture_on_open.assert_not_called()


def test_spaces_router_exposes_exact_routes():
    from backend.routers.vc_spaces import build_router

    routes = {(r.path, tuple(sorted(r.methods))) for r in build_router(runtime=_runtime()).routes}
    assert routes == {
        ("/api/version-control/spaces/{space_id}/versions", ("GET",)),
        ("/api/version-control/spaces/{space_id}/observe", ("POST",)),
    }
