"""Offline decision tests for the live promotion dependency checker."""

from types import SimpleNamespace

from backend.services.version_control.promotion.dependencies import DependencyChecker


def _checker(*, table=frozenset(), warehouse=frozenset(), folder=frozenset(),
             function=frozenset(), groups=None):
    groups = groups or {}
    return DependencyChecker(
        grantees=lambda identifier, kind, privilege: {
            "table": table, "metric_view": table, "function": function,
        }.get(kind, frozenset()),
        warehouse_users=lambda identifier: warehouse,
        folder_users=lambda identifier: folder,
        resolve_groups=lambda principal: groups.get(principal, frozenset()),
    )


_BINDING = object()
_EXEC = SimpleNamespace(principal_id="exec-sp")


def test_table_select_granted_directly():
    checker = _checker(table={"exec-sp"})
    assert checker.check(_BINDING, _EXEC, "exec-sp", "table", "cat.sch.orders") is True


def test_table_denied_when_principal_absent():
    checker = _checker(table={"someone-else"})
    assert checker.check(_BINDING, _EXEC, "exec-sp", "table", "cat.sch.orders") is False


def test_table_granted_via_group_membership():
    checker = _checker(table={"analysts"}, groups={"consumer-x": {"analysts"}})
    assert checker.check(_BINDING, _EXEC, "consumer-x", "table", "cat.sch.orders") is True


def test_warehouse_use_granted():
    checker = _checker(warehouse={"exec-sp"})
    assert checker.check(_BINDING, _EXEC, "exec-sp", "warehouse", "wh-123") is True


def test_folder_access_granted():
    checker = _checker(folder={"exec-sp"})
    assert checker.check(_BINDING, _EXEC, "exec-sp", "folder", "/Users/a/priv") is True


def test_function_execute_granted():
    checker = _checker(function={"exec-sp"})
    assert checker.check(_BINDING, _EXEC, "exec-sp", "function", "cat.sch.fn") is True


def test_unknown_kind_fails_closed():
    checker = _checker(table={"exec-sp"})
    assert checker.check(_BINDING, _EXEC, "exec-sp", "cluster", "x") is False


def test_denied_resource_fails_closed_even_with_groups():
    checker = _checker(table={"other"}, groups={"exec-sp": {"g1", "g2"}})
    assert checker.check(_BINDING, _EXEC, "exec-sp", "table", "cat.sch.orders") is False
