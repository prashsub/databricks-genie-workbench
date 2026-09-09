"""Offline orchestration contract for the D2.5b approval-minting driver.

`mint` is a thin driver over the *real* release surface (composition covered by
`test_vc_promotion_release_surface`; approval/preflight logic by the M07 suites).
Here we pin only the ordered orchestration + argument threading with a fake
surface, and the fail-closed base-observation guard.
"""

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from backend.services.version_control import contracts as vc
from scripts.version_control import mint_release_approval as driver
from scripts.version_control.mint_release_approval import MintPlan, mint

OP = "00000000-0000-4000-8000-000000000001"


def _plan():
    binding = vc.BindingRef("00000000-0000-4000-8000-0000000000bd", 1, "k", "target", "space", "prod")
    return MintPlan(
        target_binding=binding, source_version_id="ver-src",
        mapping=Mock(name="mapping"), policy=Mock(name="policy"),
        source_profile="src-profile", target_profile="tgt-profile", key="rel-key",
        requester=vc.ActorContext("requester-sp", "target", "service"),
        approvers=(vc.ActorContext("human-1", "target", "human"),
                   vc.ActorContext("human-2", "target", "human")),
        ttl_hours=4)


def _surface(*, base_busy=False, base_stale=False, base_version=True):
    executor = vc.ActorContext("exec-sp", "target", "service")
    captured = Mock(fingerprints=Mock(state_digest="digest-base")) if base_version else None
    observation = SimpleNamespace(busy=base_busy, captured_version=captured,
                                  status=SimpleNamespace(stale=base_stale))
    request = Mock(name="request")
    evidence = Mock(expected_base_fingerprints="base-fp")
    promotion = SimpleNamespace(
        target_selection=SimpleNamespace(profile="tgt-profile"),
        source_selection=SimpleNamespace(profile="src-profile"),
        observer=Mock(), preflight=Mock(return_value=evidence),
        releases=Mock(), store=Mock())
    promotion.observer.capture.return_value = observation
    identity = Mock()
    identity.executor.return_value = executor
    commands = Mock()
    commands.create.return_value = vc.OperationHandle(OP, vc.OperationStatus.REQUESTED, None)
    facts = Mock()
    facts.get_request.return_value = SimpleNamespace(request=request)
    return SimpleNamespace(identity=identity, promotion=promotion, commands=commands,
                           facts=facts, approvals=Mock()), executor, request, evidence


def test_mint_drives_create_validate_approve_promote_in_order(monkeypatch):
    surface, executor, request, evidence = _surface()
    plan = _plan()
    manifest, inputs = Mock(name="manifest"), Mock(name="inputs")
    monkeypatch.setattr(driver, "_release_manifest", lambda s, op: manifest)
    build_inputs = Mock(return_value=inputs)
    monkeypatch.setattr(driver, "build_approval_inputs", build_inputs)

    result = mint(surface, plan)

    assert result == {"operation_id": OP, "approval_id": OP, "expected_base": "digest-base"}
    # 1. base pre-observe with the resolved target executor.
    surface.identity.executor.assert_called_once_with(surface.promotion.target_selection)
    surface.promotion.observer.capture.assert_called_once_with(
        plan.target_binding, "promotion_base_observe", executor)
    # 2. create pins expected_base from the observed head + both explicit profiles.
    surface.commands.create.assert_called_once_with(
        "ver-src", plan.mapping, plan.policy, "digest-base",
        "src-profile", "tgt-profile", "rel-key", plan.requester)
    # 3. validate before any approval.
    surface.commands.validate.assert_called_once_with(OP, "rel-key", plan.requester)
    # 4. approval inputs derived from the released package + fresh preflight.
    surface.promotion.preflight.assert_called_once_with(OP, executor)
    build_inputs.assert_called_once_with(
        surface, request, plan.mapping, plan.policy, manifest, evidence,
        "base-fp", "requester-sp", ttl_hours=4)
    # 5. real approval workflow: request, two distinct human approve votes, authorize.
    surface.approvals.request.assert_called_once_with(inputs, plan.requester)
    assert surface.approvals.vote.call_args_list == [
        call(OP, "approve", plan.approvers[0]),
        call(OP, "approve", plan.approvers[1])]
    surface.approvals.authorize.assert_called_once_with(request, executor)
    # 6. promote mints the approved operation fact (approval_id == operation_id).
    surface.commands.promote.assert_called_once_with(OP, OP, "rel-key", plan.requester)


@pytest.mark.parametrize("kind", ["busy", "stale", "no_version"])
def test_mint_fails_closed_when_base_observation_unavailable(monkeypatch, kind):
    surface, *_ = _surface(base_busy=kind == "busy", base_stale=kind == "stale",
                           base_version=kind != "no_version")
    monkeypatch.setattr(driver, "_release_manifest", lambda s, op: Mock())
    monkeypatch.setattr(driver, "build_approval_inputs", Mock())
    with pytest.raises(RuntimeError, match="Base observation unavailable"):
        mint(surface, _plan())
    surface.commands.create.assert_not_called()


def _plan_config(monkeypatch):
    from scripts.version_control.build_promotion_package import build_mapping_policy
    monkeypatch.setenv("VC_ADMIN_USER", "admin@example.com")
    cfg = {
        "target_binding": {
            "binding_id": "3955d52f-0f53-44dd-b559-325060e0ec6e", "binding_revision": 1,
            "space_key": "vc-promotion-gate", "workspace_id": "111",
            "space_id": "01f1ac63c82c18389837b663f442e409", "environment": "prod"},
        "source_table": "cat.src.orders", "target_table": "cat.tgt.orders",
        "target_warehouse_id": "wh-123", "source_version_id": "ver-src"}
    mapping, _ = build_mapping_policy(cfg)
    cfg["mapping_digest"] = mapping.mapping_digest
    approval = {"requester_principal_id": "enroll-sp",
                "approvers": [{"id": "111aaa", "user_name": "a@x.com"},
                              {"id": "222bbb", "user_name": "b@x.com"}]}
    surface = SimpleNamespace(promotion=SimpleNamespace(
        source_selection=SimpleNamespace(profile="src-profile"),
        target_selection=SimpleNamespace(profile="tgt-profile")))
    return surface, cfg, approval


def test_plan_from_config_maps_real_identity_and_package_shapes(monkeypatch):
    surface, cfg, approval = _plan_config(monkeypatch)
    plan = driver._plan_from_config(surface, cfg, {"workspace_id": "111"}, approval)
    assert plan.requester == vc.ActorContext("enroll-sp", "111", "service")
    assert plan.approvers == (vc.ActorContext("111aaa", "111", "human"),
                              vc.ActorContext("222bbb", "111", "human"))
    assert plan.source_profile == "src-profile" and plan.target_profile == "tgt-profile"
    assert plan.mapping.mappings["cat.src.orders"] == "cat.tgt.orders"
    assert plan.source_version_id == "ver-src"


def test_plan_from_config_fails_closed_on_mapping_digest_drift(monkeypatch):
    surface, cfg, approval = _plan_config(monkeypatch)
    cfg["mapping_digest"] = "0" * 64  # pretend the pinned package inputs drifted
    with pytest.raises(SystemExit, match="mapping_digest"):
        driver._plan_from_config(surface, cfg, {"workspace_id": "111"}, approval)
