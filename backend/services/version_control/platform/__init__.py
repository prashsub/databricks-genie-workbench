"""Fail-closed VC/1.0 platform adapters; no ambient credentials."""

from .bundles import deployment_inventory, guard_bundle
from .provisioning import provision
from .permissions import verify_fact_permissions

from collections.abc import Callable
from threading import RLock
from typing import TypeVar, cast

from .feature_flags import FeatureFlags


Service = TypeVar("Service")


class Composition:
    def __init__(self, flags: FeatureFlags | None = None) -> None:
        self._flags = flags if flags is not None else FeatureFlags()
        self._factories: dict[type, Callable[[], object]] = {}
        self._instances: dict[type, object] = {}
        self._lock = RLock()

    @property
    def flags(self) -> FeatureFlags:
        return self._flags

    def register(self, port: type[Service], factory: Callable[[], Service]) -> None:
        with self._lock:
            if port in self._factories:
                raise ValueError(f"{port.__name__} already registered")
            self._factories[port] = factory

    def resolve(self, port: type[Service]) -> Service:
        with self._lock:
            if port not in self._instances:
                self._instances[port] = self._factories[port]()
            return cast(Service, self._instances[port])


def compose(factories=None, *, flags: FeatureFlags | None = None) -> Composition:
    container = Composition(flags)
    for port, factory in (factories or {}).items():
        container.register(port, factory)
    return container
