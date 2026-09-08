from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID

import pytest

from backend.services.version_control import contracts as vc
from backend.tests.vc_fakes.fixtures import FakeCanonicalizer


def version_id(number):
    return str(UUID(int=number))


def snapshot(description):
    return FakeCanonicalizer().observe({"serialized_space": {}, "description": description})


def lookup(states):
    port = Mock(spec=vc.VersionLookup)
    port.get.side_effect = states.__getitem__
    return port


@pytest.mark.parametrize("observed,approved,deployed,expected", [
    (1, 1, 1, "clean"), (2, 1, 1, "external_ahead"),
    (1, 2, 1, "desired_ahead"), (2, 3, 1, "diverged"),
    (2, 2, 1, "desired_ahead"),
])
def test_three_heads_classify_clean_external_desired_diverged(observed, approved, deployed, expected):
    from backend.services.version_control.drift import DriftService

    versions = lookup({version_id(number): snapshot(str(number)) for number in (1, 2, 3)})
    result = DriftService(canonicalizer=FakeCanonicalizer()).classify(
        vc.Heads(*(version_id(number) for number in (observed, approved, deployed))), versions, "reachable")
    assert result.state.value == expected
    assert result.policy_target == version_id(approved)
    assert result.common_base == (version_id(1) if observed == 1 or approved == 1 else None)
    assert result.reasons


@pytest.mark.parametrize("case", ["missing_head", "missing_history", "unsupported", "mixed"])
def test_missing_history_or_incompatible_canonicalizer_is_unknown(case):
    from backend.services.version_control.drift import DriftService

    original = snapshot("one")
    unsupported = replace(original, fingerprints=replace(original.fingerprints, canonicalizer_version="future/9"))
    states = {version_id(1): original, version_id(2): original}
    heads = vc.Heads(version_id(1), version_id(2), version_id(1))
    if case == "missing_head":
        heads = replace(heads, approved=None)
    elif case == "missing_history":
        del states[version_id(2)]
    elif case == "unsupported":
        states = dict.fromkeys(states, unsupported)
    else:
        states[version_id(2)] = unsupported
    result = DriftService(canonicalizer=FakeCanonicalizer()).classify(heads, lookup(states), "reachable")
    assert result.state == vc.DriftState.UNKNOWN
    assert result.allowed_actions == ()
    assert result.reasons


@pytest.mark.parametrize("change", ["metadata", "array_order"])
def test_meaningful_changes_are_not_clean(change):
    from backend.services.version_control.drift import DriftService

    canonicalizer = FakeCanonicalizer()
    before = canonicalizer.observe({"serialized_space": {"queries": ["first", "second"]}, "description": "old"})
    after = canonicalizer.observe({"serialized_space": {"queries": ["second", "first"] if change == "array_order"
                                                    else ["first", "second"]}, "description": "new"})
    result = DriftService(canonicalizer=canonicalizer).classify(
        vc.Heads(version_id(2), version_id(1), version_id(1)),
        lookup({version_id(1): before, version_id(2): after}), "reachable")
    assert result.state == vc.DriftState.EXTERNAL_AHEAD


@pytest.mark.parametrize("reachability,unresolved,quarantined,conflicted,expected", [
    ("unreachable", None, False, False, "unreachable"),
    ("reachable", version_id(9), False, False, "applied_unverified"),
    ("reachable", None, True, False, "unknown"),
    ("reachable", None, False, True, "conflicted"),
    ("unreachable", version_id(9), True, True, "unreachable"),
    ("unknown", None, False, False, "unknown"),
])
def test_unreachable_and_unverified_override_ordinary_badges(
        reachability, unresolved, quarantined, conflicted, expected):
    from backend.services.version_control.drift import DriftService

    versions = lookup({version_id(1): snapshot("same")})
    result = DriftService(canonicalizer=FakeCanonicalizer()).classify(
        vc.Heads(*(version_id(1),) * 3), versions, reachability,
        unresolved_operation_id=unresolved, quarantined=quarantined, conflicted=conflicted)
    assert result.state.value == expected
    assert result.quarantined is quarantined
    assert result.unresolved_operation_id == unresolved
    assert result.conflicted is conflicted
    assert result.allowed_actions == ()
    assert result.reasons
    versions.get.assert_not_called()


def projection_setup():
    from backend.services.version_control.drift import DriftService
    from backend.services.version_control.drift.ports import CoordinationReadiness, Projections
    from backend.tests.test_vc_reconcile import NOW, observed_result
    from backend.tests.vc_fakes.fixtures import binding_fixture

    projections = Mock(spec=Projections)
    projections.page.return_value = vc.OverviewPage((observed_result().status,), "next")
    projections.get.return_value = observed_result().status
    observer = Mock(spec=vc.Observer)
    registry = Mock(spec=vc.Registry)
    registry.resolve.return_value = binding_fixture()
    authorize = Mock(return_value=True)
    service = DriftService(canonicalizer=FakeCanonicalizer(), projections=projections, observer=observer,
                           registry=registry, authorize_history=authorize, clock=lambda: NOW,
                           readiness=Mock(spec=CoordinationReadiness))
    return service, projections, observer, registry, authorize


def test_overview_uses_projection_without_per_row_genie_calls():
    from backend.tests.vc_fakes.fixtures import actor_fixture, binding_fixture

    service, projections, observer, registry, authorize = projection_setup()
    page = service.overview(actor_fixture(), "cursor", 2)
    projections.page.assert_called_once_with(actor_fixture(), "cursor", 2)
    assert page.next_cursor == "next"
    assert page.items[0].projection_as_of == projections.page.return_value.items[0].projection_as_of
    assert page.items[0].allowed_actions == ("compare",)
    assert page.items[0].drift == vc.DriftState.EXTERNAL_AHEAD
    assert service.status(binding_fixture(), actor_fixture()) == page.items[0]
    observer.capture.assert_not_called()
    observer.capture_on_open.assert_not_called()
    service.reconcile_enabled = True
    assert "adopt" in service.overview(actor_fixture()).items[0].allowed_actions
    assert "reapply" not in service.overview(actor_fixture()).items[0].allowed_actions


@pytest.mark.parametrize("case", ["stale", "denied", "wrong_workspace", "oversized", "limit"])
def test_projection_paging_freshness_and_scope_are_fail_closed(case):
    from datetime import timedelta
    from backend.tests.test_vc_reconcile import NOW
    from backend.tests.vc_fakes.fixtures import actor_fixture, binding_fixture

    service, projections, observer, registry, authorize = projection_setup()
    if case == "stale":
        service.clock = lambda: NOW + timedelta(hours=1)
        status = service.overview(actor_fixture()).items[0]
        assert status.stale and status.allowed_actions == ()
        assert status.observed_at == NOW
    elif case == "denied":
        authorize.return_value = False
        with pytest.raises(PermissionError):
            service.overview(actor_fixture())
    elif case == "wrong_workspace":
        registry.resolve.return_value = replace(binding_fixture(), workspace_id="other")
        with pytest.raises(PermissionError):
            service.overview(actor_fixture())
    elif case == "oversized":
        projections.page.return_value = replace(projections.page.return_value,
                                                items=projections.page.return_value.items * 3)
        with pytest.raises(ValueError):
            service.overview(actor_fixture(), limit=2)
    else:
        with pytest.raises(ValueError):
            service.overview(actor_fixture(), limit=101)
    observer.capture.assert_not_called()


def test_projection_routes_auth_paging_status_and_observer_refresh_boundary():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.routers.vc_reconcile import build_router
    from backend.tests.vc_fakes.fixtures import actor_fixture, binding_fixture

    service, projections, observer, registry, authorize = projection_setup()
    identity = Mock(spec=vc.IdentityProvider)
    identity.actor.return_value = actor_fixture()
    app = FastAPI()
    authenticated = [False]
    @app.middleware("http")
    async def authenticate(request, call_next):
        if authenticated[0]:
            request.state.vc_auth = vc.AuthenticatedRequest("test", "123")
        return await call_next(request)
    app.include_router(build_router(service=service, identity=identity, registry=registry,
                                    authorize_history=authorize))
    client = TestClient(app)
    assert client.get("/api/version-control/overview").status_code == 401
    authenticated[0] = True
    response = client.get("/api/version-control/overview?limit=2&cursor=opaque")
    assert response.status_code == 200
    assert response.json()["next_cursor"] == "next"
    assert response.json()["items"][0]["projection_as_of"].endswith("Z")
    path = f"/api/version-control/bindings/{binding_fixture().binding_id}"
    assert client.get(path + "/status").status_code == 200
    assert client.get("/api/version-control/overview?limit=101").status_code == 422
    assert path + "/observe" not in {route.path for route in app.routes}
    observer.capture.assert_not_called()


def rendered_setup():
    from backend.services.version_control.drift import DriftService
    from backend.services.version_control.drift.ports import ApprovedTarget, ApprovedTargets
    from backend.tests.test_vc_reconcile import NOW, reconcile_setup, reconcile_request
    from backend.tests.vc_fakes.fixtures import actor_fixture, binding_fixture

    service, observer, ledger, policy, approvals, identity = reconcile_setup()
    source = ledger.get_version(binding_fixture(), version_id(1))
    base = ledger.get_version(binding_fixture(), version_id(2))
    rendered = snapshot("target-environment")
    inputs = replace(policy.inputs(binding_fixture(), reconcile_request("reapply"), source, base, actor_fixture()),
                     rendered_target_digest=rendered.state_digest, mapping_digest="e" * 64,
                     transformer_version="map/1")
    record = vc.ApprovalRecord(vc.ApprovalRequest(version_id(20), inputs, NOW), (), vc.FactStatus.APPROVED, "f" * 64)
    preflight = vc.PreflightEvidence("VC/1.0", inputs.operation_id, binding_fixture(),
                                     base.snapshot.fingerprints, rendered.fingerprints, None,
                                     "b" * 64, "c" * 64, inputs.preflight_evidence_digest, NOW)
    targets = Mock(spec=ApprovedTargets)
    targets.resolve.return_value = ApprovedTarget(version_id(1), record, rendered, preflight)
    service = DriftService(canonicalizer=FakeCanonicalizer(), approved_targets=targets)
    versions = lookup({version_id(1): source.snapshot, version_id(2): rendered})
    return service, targets, versions


def test_target_approval_uses_rendered_fingerprint_not_source_environment_hash():
    from backend.tests.vc_fakes.fixtures import binding_fixture

    service, targets, versions = rendered_setup()
    result = service.classify(vc.Heads(version_id(2), version_id(1), version_id(2)), versions,
                              "reachable", target_binding=binding_fixture())
    assert result.state == vc.DriftState.CLEAN
    assert result.policy_target == version_id(1)
    targets.resolve.assert_called_once_with(binding_fixture(), version_id(1))
    assert versions.get(version_id(1)).fingerprints != targets.resolve.return_value.rendered.fingerprints


@pytest.mark.parametrize("failure", ["missing", "wrong_binding", "not_approved", "wrong_digest",
                                    "wrong_preflight", "wrong_head", "incompatible", "no_target"])
def test_rendered_target_requires_bound_approved_evidence(failure):
    from backend.tests.vc_fakes.fixtures import binding_fixture

    service, targets, versions = rendered_setup()
    evidence = targets.resolve.return_value
    if failure == "missing":
        targets.resolve.side_effect = LookupError("missing target evidence")
    elif failure == "wrong_binding":
        inputs = replace(evidence.approval.request.inputs, target_binding=replace(binding_fixture(), space_id="other"))
        evidence = replace(evidence, approval=replace(evidence.approval, request=replace(evidence.approval.request, inputs=inputs)))
    elif failure == "not_approved":
        evidence = replace(evidence, approval=replace(evidence.approval, status=vc.FactStatus.REQUESTED))
    elif failure == "wrong_digest":
        evidence = replace(evidence, rendered=snapshot("tampered"))
    elif failure == "wrong_preflight":
        evidence = replace(evidence, preflight=replace(evidence.preflight, preflight_evidence_digest="0" * 64))
    elif failure == "wrong_head":
        evidence = replace(evidence, head_id=version_id(99))
    elif failure == "incompatible":
        evidence = replace(evidence, rendered=replace(evidence.rendered, fingerprints=replace(
            evidence.rendered.fingerprints, canonicalizer_version="future/9")))
    targets.resolve.return_value = evidence
    result = service.classify(vc.Heads(version_id(2), version_id(1), version_id(2)), versions,
                              "reachable", target_binding=None if failure == "no_target" else binding_fixture())
    assert result.state == vc.DriftState.UNKNOWN
    assert result.allowed_actions == ()


@pytest.mark.parametrize("explicit_none", [False, True])
@pytest.mark.parametrize("observed", [1, 2])
def test_classification_fails_closed_when_approved_targets_absent(explicit_none, observed):
    from backend.services.version_control.drift import DriftService
    from backend.tests.vc_fakes.fixtures import binding_fixture

    canonicalizer = Mock(wraps=FakeCanonicalizer())
    service = DriftService(canonicalizer=canonicalizer, **({"approved_targets": None} if explicit_none else {}))
    versions = lookup({version_id(1): snapshot("policy"), version_id(2): snapshot("external")})
    result = service.classify(vc.Heads(version_id(observed), version_id(1), version_id(1)),
                              versions, "reachable", target_binding=binding_fixture())
    assert result.state == vc.DriftState.UNKNOWN
    assert result.allowed_actions == ()
    assert "rendered approval" in " ".join(result.reasons).lower()
    canonicalizer.compare.assert_not_called()


@pytest.mark.parametrize("view", ["status", "overview"])
def test_projected_badge_fails_closed_when_approved_targets_absent(view):
    from backend.tests.vc_fakes.fixtures import actor_fixture, binding_fixture

    service, projections, observer, registry, authorize = projection_setup()
    service.approved_targets = None
    service.reconcile_enabled = True
    service.dispatch_enabled = True
    clean = replace(projections.get.return_value, drift=vc.DriftState.CLEAN)
    projections.get.return_value = clean
    projections.page.return_value = vc.OverviewPage((clean,), None)
    status = (service.status(binding_fixture(), actor_fixture()) if view == "status"
              else service.overview(actor_fixture()).items[0])
    assert status.drift == vc.DriftState.UNKNOWN
    assert status.allowed_actions == ()
    assert status.heads == clean.heads
    assert "rendered approval" in " ".join(status.reasons).lower()
    observer.capture.assert_not_called()
