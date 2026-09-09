"""A4: resolve_governed_ports pure builder (offline, fake seams).

Verifies the builder fixes the dependency order, renders flags-from-config, pins
verify_only to the constructed gate+executor and attempt_state to the durable
facts, wires optional handlers only when their seam is provided, and fails
closed on incomplete config.
"""

from types import SimpleNamespace

import pytest

from backend.jobs import GovernedSeams, resolve_governed_ports

OP = "00000000-0000-4000-8000-000000000011"


def _facts_new():
    """A durable-facts fake whose history classifies every op as ``new``."""
    binding = SimpleNamespace(binding_id="b", binding_revision=1, workspace_id="target")
    identity = SimpleNamespace(idempotency_key="idem")
    request = SimpleNamespace(binding=binding, identity=identity)
    facts = SimpleNamespace(kind="facts")
    facts.get_request = lambda _op: SimpleNamespace(request=request)
    facts.lookup_request = lambda _b, _k: SimpleNamespace(facts=(), ambiguous=False)
    return facts


def _config(**overrides):
    config = {
        "workspace_id": "target",
        "catalog": "sandbox_cat",
        "control_schema": "vc_ctl",
        "warehouse_id": "wh-123",
        "principals": {"runtime": "sp-1"},
        "flags": {"vc_writes_enabled": True, "vc_restore_enabled": True},
    }
    config.update(overrides)
    return config


def _seams(*, calls, gate, executor, facts, with_restore=False, with_optimizer=False,
           with_reconcile=False, with_dispatcher=False):
    def record(name, value):
        def factory(*args, **kwargs):
            calls.append((name, args, kwargs))
            return value
        return factory

    dispatcher = SimpleNamespace(kind="dispatcher") if with_dispatcher else None
    kwargs = {
        "facts": record("facts", facts),
        "ledger": record("ledger", SimpleNamespace(kind="ledger")),
        "registry": record("registry", SimpleNamespace(kind="registry")),
        "canonicalizer": record("canonicalizer", SimpleNamespace(kind="canon")),
        "identity": record("identity", SimpleNamespace(kind="identity")),
        "executor": record("executor", executor),
        "coordination": record("coordination", SimpleNamespace(kind="coord")),
        "approvals": record("approvals", SimpleNamespace(kind="approvals")),
        "transport": record("transport", SimpleNamespace(kind="transport")),
        "gate": record("gate", gate),
        "capability_probe": record("capability_probe", lambda: {"ok": True}),
    }
    if with_dispatcher:
        kwargs["dispatcher"] = record("dispatcher", dispatcher)
    if with_restore:
        kwargs["restore"] = record("restore", SimpleNamespace(kind="restore"))
    if with_optimizer:
        kwargs["optimizer"] = record("optimizer", SimpleNamespace(kind="optimizer"))
    if with_reconcile:
        kwargs["reconcile"] = record("reconcile", SimpleNamespace(kind="reconcile"))
    return GovernedSeams(**kwargs)


def test_returns_core_assemble_kwargs():
    calls, gate = [], SimpleNamespace(kind="gate")
    executor = SimpleNamespace(kind="executor", workspace_id="target")
    facts = _facts_new()
    ports = resolve_governed_ports(
        config=_config(), seams=_seams(calls=calls, gate=gate, executor=executor, facts=facts))
    assert ports["workspace_id"] == "target"
    assert ports["facts"] is facts
    assert ports["gate"] is gate
    assert ports["executor"] is executor
    assert callable(ports["verify_only"])
    assert callable(ports["attempt_state"])
    assert ports["capability_probe"]() == {"ok": True}
    # Optional handlers absent unless their seam is supplied.
    assert "restore_service" not in ports
    assert "optimizer_adapter" not in ports
    assert "reconcile_service" not in ports


def test_flags_are_rendered_from_config():
    calls, gate = [], SimpleNamespace()
    ports = resolve_governed_ports(
        config=_config(), seams=_seams(calls=calls, gate=gate,
                                       executor=SimpleNamespace(), facts=_facts_new()))
    flags = ports["flags"]
    assert flags.enabled("vc_restore_enabled") is True
    assert flags.vc_promotion_enabled is False


def test_verify_only_is_pinned_to_gate_and_executor():
    calls = []
    gate = SimpleNamespace()
    gate.verify_only = lambda operation_id, executor: ("verified", operation_id, executor)
    executor = SimpleNamespace(kind="executor")
    ports = resolve_governed_ports(
        config=_config(), seams=_seams(calls=calls, gate=gate, executor=executor,
                                       facts=_facts_new()))
    assert ports["verify_only"](OP) == ("verified", OP, executor)


def test_attempt_state_uses_durable_facts():
    calls, gate = [], SimpleNamespace()
    facts = _facts_new()
    ports = resolve_governed_ports(
        config=_config(), seams=_seams(calls=calls, gate=gate,
                                       executor=SimpleNamespace(), facts=facts))
    assert ports["attempt_state"](OP) == "new"


def test_dependency_order_is_respected():
    calls, gate = [], SimpleNamespace()
    resolve_governed_ports(
        config=_config(), seams=_seams(calls=calls, gate=gate,
                                       executor=SimpleNamespace(), facts=_facts_new()))
    order = [name for name, _a, _k in calls]
    # facts/ledger/registry precede coordination; coordination precedes gate.
    assert order.index("facts") < order.index("coordination")
    assert order.index("ledger") < order.index("coordination")
    assert order.index("registry") < order.index("coordination")
    assert order.index("coordination") < order.index("gate")
    assert order.index("approvals") < order.index("gate")
    assert order.index("transport") < order.index("gate")


def test_handlers_wired_only_when_seams_present():
    calls, gate = [], SimpleNamespace()
    facts = _facts_new()
    seams = _seams(calls=calls, gate=gate, executor=SimpleNamespace(), facts=facts,
                   with_restore=True, with_optimizer=True, with_reconcile=True,
                   with_dispatcher=True)
    ports = resolve_governed_ports(config=_config(), seams=seams)
    assert ports["restore_service"].kind == "restore"
    assert ports["optimizer_adapter"].kind == "optimizer"
    assert ports["reconcile_service"].kind == "reconcile"
    # restore seam receives the constructed gate + dispatcher.
    restore_call = next(k for name, _a, k in calls if name == "restore")
    assert restore_call["gate"] is gate
    assert restore_call["dispatcher"].kind == "dispatcher"


@pytest.mark.parametrize("missing", ("workspace_id", "catalog",
                         "control_schema", "warehouse_id", "principals"))
def test_fails_closed_on_incomplete_config(missing):
    calls, gate = [], SimpleNamespace()
    seams = _seams(calls=calls, gate=gate, executor=SimpleNamespace(), facts=_facts_new())
    broken = {k: v for k, v in _config().items() if k != missing}
    with pytest.raises(PermissionError, match="not integrated"):
        resolve_governed_ports(config=broken, seams=seams)


def test_fails_closed_on_empty_config():
    calls, gate = [], SimpleNamespace()
    seams = _seams(calls=calls, gate=gate, executor=SimpleNamespace(), facts=_facts_new())
    with pytest.raises(PermissionError, match="not integrated"):
        resolve_governed_ports(config=None, seams=seams)
