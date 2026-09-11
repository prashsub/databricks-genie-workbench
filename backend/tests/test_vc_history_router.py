"""VC version-history read router: flag-gated, scoped, delegates to the ledger.

History is a read surface: it is gated on ``vc_history_enabled`` only and must never
require the write switches, so an operator can inspect what the optimizer changed even
while governed writes stay off.
"""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.version_control import contracts as vc
from backend.tests.test_vc_mutation_gate import rig, uid
from backend.tests.vc_fakes.fixtures import actor_fixture


@pytest.fixture
def history_rig(rig):
    from backend.routers.vc_history import build_router

    now = datetime.now(timezone.utc)
    fingerprints = vc.Fingerprints("a" * 64, "b" * 64, "c" * 64, "vc-c14n/1")
    summary = vc.VersionSummary(uid(), rig.binding.binding_id, now, vc.Origin.EXTERNAL,
        "user@example.com", None, None, fingerprints, "run-1", None)
    page = vc.VersionPage((summary,), "cursor-2")
    ledger = Mock(spec=vc.VersionLedger)
    ledger.history.return_value = page
    actor = actor_fixture()
    identity = Mock(spec=vc.IdentityProvider)
    identity.actor.return_value = actor
    registry = Mock(spec=vc.Registry)
    registry.resolve.return_value = rig.binding
    authorize_history = Mock(return_value=True)
    authentication = vc.AuthenticatedRequest("verified-session", actor.workspace_id)

    def make_client(flags=rig.flags, authenticated=True):
        app = FastAPI()

        @app.middleware("http")
        async def authenticate(request, call_next):
            if authenticated:
                request.state.vc_auth = authentication
            return await call_next(request)

        router = build_router(ledger=ledger, registry=registry, identity=identity,
            authorize_history=authorize_history, flags=flags)
        app.include_router(router)
        return TestClient(app), router

    client, router = make_client()
    prefix = f"/api/version-control/bindings/{rig.binding.binding_id}"
    return SimpleNamespace(**locals())


def test_versions_delegates_to_ledger_history(history_rig):
    setup = history_rig
    response = setup.client.get(setup.prefix + "/versions?limit=5")
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["version_id"] == setup.summary.version_id
    assert body["items"][0]["optimizer_run_id"] == "run-1"
    assert body["next_cursor"] == "cursor-2"
    setup.ledger.history.assert_called_once_with(setup.rig.binding, None, 5)
    setup.authorize_history.assert_called_once_with(setup.actor, setup.rig.binding)
    setup.identity.actor.assert_called_once_with(setup.authentication)


def test_versions_forwards_cursor_and_default_limit(history_rig):
    setup = history_rig
    assert setup.client.get(setup.prefix + "/versions?cursor=abc").status_code == 200
    setup.ledger.history.assert_called_once_with(setup.rig.binding, "abc", 25)


@pytest.mark.parametrize("flags", ["default", "disabled"])
def test_history_is_fail_closed_when_flag_off(history_rig, flags):
    setup = history_rig
    if flags == "disabled":
        setup.rig.flags.enabled.side_effect = lambda switch: switch != "vc_history_enabled"
    client, _ = setup.make_client(flags=None if flags == "default" else setup.rig.flags)
    response = client.get(setup.prefix + "/versions")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "vc_history_disabled"
    setup.ledger.history.assert_not_called()


@pytest.mark.parametrize("denial,code", [("authentication", 401), ("history", 403), ("workspace", 403)])
def test_history_requires_server_identity_scope(history_rig, denial, code):
    setup = history_rig
    client, _ = setup.make_client(authenticated=denial != "authentication")
    if denial == "history":
        setup.authorize_history.return_value = False
    if denial == "workspace":
        setup.identity.actor.return_value = replace(setup.actor, workspace_id="other")
    response = client.get(setup.prefix + "/versions")
    assert response.status_code == code
    setup.ledger.history.assert_not_called()


@pytest.mark.parametrize("limit", [0, 101])
def test_history_rejects_out_of_range_limit(history_rig, limit):
    setup = history_rig
    assert setup.client.get(setup.prefix + f"/versions?limit={limit}").status_code == 422
    setup.ledger.history.assert_not_called()


def test_history_router_exposes_exact_contract_route(history_rig):
    assert {(route.path, tuple(sorted(route.methods))) for route in history_rig.router.routes} == {
        ("/api/version-control/bindings/{binding_id}/versions", ("GET",)),
    }
