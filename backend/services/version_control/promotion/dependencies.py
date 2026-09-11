"""Live target-dependency and permission checker for promotion preflight.

`PromotionService.preflight` refuses to proceed unless every principal that will
touch the promoted space (the executor and each declared consumer) can actually
use every target resource the rendered config references — the target warehouse,
the destination folder, and each structured identifier (table / metric view /
catalog / schema / function). This is the live implementation of that
`dependencies.check(binding, executor, principal, kind, identifier)` port.

The decision core is a pure set-membership test over injected privilege probes,
so it is offline-testable; the credentialed probes (UC `SHOW GRANTS`, warehouse
and workspace-folder permission reads, SCIM group expansion) live in the seam
factory that binds this checker to a `WorkspaceClient`. A principal is
authorized when it — or any group it belongs to — holds the required privilege
on the resource; an unrecognized resource kind fails closed.
"""

from collections.abc import Callable
from typing import Any, ClassVar


class DependencyChecker:
    # UC privilege required to *use* (not administer) each securable kind. ALL
    # PRIVILEGES is accepted by the grantee probe as a superset.
    _REQUIRED: ClassVar[dict[str, str]] = {
        "table": "SELECT",
        "metric_view": "SELECT",
        "catalog": "USE CATALOG",
        "schema": "USE SCHEMA",
        "function": "EXECUTE",
    }

    def __init__(self, *, grantees: Callable[[str, str, str], frozenset[str]],
                 warehouse_users: Callable[[str], frozenset[str]],
                 folder_users: Callable[[str], frozenset[str]],
                 resolve_groups: Callable[[str], frozenset[str]]):
        self._grantees = grantees
        self._warehouse_users = warehouse_users
        self._folder_users = folder_users
        self._resolve_groups = resolve_groups

    def check(self, binding: Any, executor: Any, principal: str, kind: str, identifier: str) -> bool:
        authorized = {principal, *self._resolve_groups(principal)}
        if kind == "warehouse":
            return bool(authorized & self._warehouse_users(identifier))
        if kind == "folder":
            return bool(authorized & self._folder_users(identifier))
        privilege = self._REQUIRED.get(kind)
        if privilege is None:
            return False
        return bool(authorized & self._grantees(identifier, kind, privilege))
