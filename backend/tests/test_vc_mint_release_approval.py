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
