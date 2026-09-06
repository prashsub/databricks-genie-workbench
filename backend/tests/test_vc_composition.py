"""Offline composition scaffold; never mounts routes or enables writers."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import importlib

import pytest


WRITE_SWITCHES = {
    "vc_writes_enabled", "vc_restore_enabled", "vc_promotion_enabled",
    "vc_optimizer_apply_enabled", "vc_reconcile_enabled",
}


def test_modules_are_wired_once_with_write_switch_default_off(monkeypatch):
    package = importlib.import_module("backend.services.version_control.platform")
    flags_module = importlib.import_module("backend.services.version_control.platform.feature_flags")
    for name in WRITE_SWITCHES:
        monkeypatch.setenv(name.upper(), "true")
    flags = flags_module.FeatureFlags()
    assert flags_module.WRITE_SWITCHES == WRITE_SWITCHES
    assert all(getattr(flags, name) is False for name in WRITE_SWITCHES)
    assert all(flags.enabled(name) is False for name in WRITE_SWITCHES)
    assert all(value is False for name, value in flags.registry.items() if name in WRITE_SWITCHES)
    with pytest.raises(FrozenInstanceError):
        flags.vc_writes_enabled = True
    with pytest.raises(TypeError):
        flags.registry["vc_writes_enabled"] = True
    with pytest.raises((ValueError, TypeError)):
        replace(flags, vc_writes_enabled="false")
    with pytest.raises(KeyError):
        flags.enabled("unknown_writer")
    for name in WRITE_SWITCHES - {"vc_writes_enabled"}:
        assert replace(flags, **{name: True}).enabled(name) is False
    assert replace(flags, vc_history_enabled=True).enabled("vc_writes_enabled") is False

    contract = importlib.import_module("backend.services.version_control.contracts")
    composition = package.Composition()
    instance = object()
    calls = []

    def factory():
        calls.append("created")
        return instance

    composition.register(contract.Canonicalizer, factory)
    assert calls == []
    with ThreadPoolExecutor(max_workers=4) as workers:
        resolved = list(workers.map(lambda _: composition.resolve(contract.Canonicalizer), range(8)))
    assert all(value is instance for value in resolved)
    assert calls == ["created"]
    with pytest.raises(ValueError, match="already"):
        composition.register(contract.Canonicalizer, factory)
    with pytest.raises(KeyError):
        composition.resolve(contract.MutationGate)
    assert all(composition.flags.enabled(name) is False for name in WRITE_SWITCHES)
    assert importlib.import_module(package.__name__) is package
