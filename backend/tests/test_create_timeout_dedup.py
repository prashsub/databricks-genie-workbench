"""Pin M04's fail-closed legacy create and isolated timeout/title helpers.

Display-name-match create-on-timeout reconciliation is RETIRED in favor of
durable create-intent + gated create (see test_vc_create.py). Legacy create
must reject calls before any POST, blind replay, listing, or title adoption,
including adoption of a pre-existing same-named space (a data-loss risk).

The timeout/title helpers remain tested in isolation, not as an active create
path. Hidden SDK retry prevention belongs to test_genie_client.py's
test_transport_disables_hidden_sdk_retries_for_post_and_both_patches.
"""

from unittest.mock import Mock

import pytest

import backend.genie_creator as gc
import backend.services.genie_client as gcli


_DISABLED_CREATE = "VC writes disabled: create requires durable intent and the mutation gate"


class _FakeApiClient:
    def __init__(self, created_title, created_id="sp_reconciled", create_time="2026-08-12T12:03:55Z"):
        self._title = created_title
        self._id = created_id
        self._ct = create_time
        self.post_calls = 0

    def do(self, method, path, body=None, query=None):
        if method == "POST" and path == "/api/2.0/genie/spaces":
            self.post_calls += 1
            raise Exception(
                "HTTPSConnectionPool(host='8259553952209604.4.gcp.databricks.com', "
                "port=443): Read timed out."
            )
        if method == "GET" and path == "/api/2.0/genie/spaces":
            return {"spaces": [
                {"space_id": self._id, "title": self._title, "create_time": self._ct},
                {"space_id": "sp_other", "title": "Some Unrelated Space", "create_time": self._ct},
            ], "next_page_token": None}
        raise AssertionError(f"unexpected: {method} {path}")


class _FakeClient:
    def __init__(self, api):
        self.api_client = api


def _patch(monkeypatch, api):
    monkeypatch.setattr(api, "do", Mock(wraps=api.do))
    monkeypatch.setattr(gc, "get_workspace_client", Mock(return_value=_FakeClient(api)))
    monkeypatch.setattr(gc, "get_databricks_host", lambda: "https://example.cloud.databricks.com")
    monkeypatch.setattr(gc, "get_sql_warehouse_id", lambda: "wh_123")
    monkeypatch.setattr(gc, "_non_retrying_client", Mock(side_effect=lambda base: base))
    monkeypatch.setattr(gc, "_title_matches_requested", Mock(wraps=gc._title_matches_requested))
    monkeypatch.setattr(gcli, "list_genie_spaces",
                        Mock(side_effect=lambda: api.do("GET", "/api/2.0/genie/spaces")["spaces"]))


def _assert_no_legacy_activity(api):
    api.do.assert_not_called()
    assert api.post_calls == 0
    gc.get_workspace_client.assert_not_called()
    gc._non_retrying_client.assert_not_called()
    gcli.list_genie_spaces.assert_not_called()
    gc._title_matches_requested.assert_not_called()


# ── _is_timeout_error ────────────────────────────────────────────────────────

def test_is_timeout_error_detects_read_timed_out():
    assert gc._is_timeout_error(Exception("HTTPSConnectionPool(...): Read timed out."))
    assert gc._is_timeout_error(Exception("connect timeout"))
    assert gc._is_timeout_error(TimeoutError("x"))


def test_is_timeout_error_false_for_non_timeout():
    assert not gc._is_timeout_error(Exception("403 permission denied"))
    assert not gc._is_timeout_error(ValueError("bad config"))


# ── _title_matches_requested ─────────────────────────────────────────────────

def test_title_matcher_exact_and_variants():
    assert gc._title_matches_requested("Sales", "Sales")
    assert gc._title_matches_requested("Sales 2026-08-12 12:03:55", "Sales")
    assert gc._title_matches_requested("Sales 2026-08-10 13:17", "Sales")           # no seconds
    assert gc._title_matches_requested("Sales [2026-08-10 13:17]", "Sales")         # brackets
    assert gc._title_matches_requested("Sales 2026-08-12T12:03:55.123Z", "Sales")   # iso+frac+Z


def test_title_matcher_rejects_prefix_siblings():
    for t in ["Sales Report", "Sales Agent", "SalesX", "Sales v2", "Sales 2026"]:
        assert not gc._title_matches_requested(t, "Sales"), t


def test_legacy_create_rejects_exact_title_adoption(monkeypatch):
    title = "Ops_Collection_team_SQL_Agent"
    api = _FakeApiClient(created_title=title, created_id="sp_ok", create_time="2999-01-01T00:00:00Z")
    _patch(monkeypatch, api)
    with pytest.raises(PermissionError, match=_DISABLED_CREATE):
        gc.create_genie_space(display_name=title, merged_config={"data_sources": {"tables": []}})
    _assert_no_legacy_activity(api)


def test_legacy_create_rejects_timestamp_renamed_adoption(monkeypatch):
    requested = "Ops_Collection_team_SQL_Agent"

    class _RenamedApi(_FakeApiClient):
        def do(self, method, path, body=None, query=None):
            if method == "POST":
                self.post_calls += 1
                raise Exception("HTTPSConnectionPool(host='x'): Read timed out.")
            return {"spaces": [
                {"space_id": "sp_renamed", "title": f"{requested} 2999-08-10 13:04:12",
                 "create_time": "2999-08-10T13:04:12Z"},
                {"space_id": "sp_unrelated", "title": "Something Else", "create_time": "2999-01-01T00:00:00Z"},
            ], "next_page_token": None}

    api = _RenamedApi(created_title=requested)
    _patch(monkeypatch, api)
    with pytest.raises(PermissionError, match=_DISABLED_CREATE):
        gc.create_genie_space(display_name=requested, merged_config={"data_sources": {"tables": []}})
    _assert_no_legacy_activity(api)


def test_legacy_create_never_adopts_preexisting_same_named_space(monkeypatch):
    """CRITICAL (data-loss guard): a same-named space created BEFORE this attempt
    must NOT be adopted — otherwise a later update_space overwrites it. Legacy
    create must fail closed before listing spaces or invoking title matching."""
    requested = "Sales Agent"

    class _OldOnlyApi(_FakeApiClient):
        def do(self, method, path, body=None, query=None):
            if method == "POST":
                self.post_calls += 1
                raise Exception("Read timed out.")
            # Only a pre-existing space with an OLD create_time exists.
            return {"spaces": [
                {"space_id": "sp_preexisting", "title": requested, "create_time": "2000-01-01T00:00:00Z"},
            ], "next_page_token": None}

    api = _OldOnlyApi(created_title=requested)
    _patch(monkeypatch, api)
    with pytest.raises(PermissionError, match=_DISABLED_CREATE):
        gc.create_genie_space(display_name=requested, merged_config={"data_sources": {"tables": []}})
    _assert_no_legacy_activity(api)


def test_legacy_create_never_selects_between_same_named_spaces(monkeypatch):
    requested = "Sales Agent"

    class _BothApi(_FakeApiClient):
        def do(self, method, path, body=None, query=None):
            if method == "POST":
                self.post_calls += 1
                raise Exception("Read timed out.")
            return {"spaces": [
                {"space_id": "sp_dup", "title": f"{requested} 2999-08-10 13:05:00",
                 "create_time": "2999-08-10T13:05:00Z"},
                {"space_id": "sp_original", "title": requested,
                 "create_time": "2999-08-10T13:04:00Z"},
            ], "next_page_token": None}

    api = _BothApi(created_title=requested)
    _patch(monkeypatch, api)
    with pytest.raises(PermissionError, match=_DISABLED_CREATE):
        gc.create_genie_space(display_name=requested, merged_config={"data_sources": {"tables": []}})
    _assert_no_legacy_activity(api)


def test_legacy_create_without_match_rejects_blind_replay(monkeypatch):
    class _NoMatchApi(_FakeApiClient):
        def do(self, method, path, body=None, query=None):
            if method == "POST":
                self.post_calls += 1
                raise Exception("Read timed out.")
            return {"spaces": [{"space_id": "x", "title": "A Different Title",
                                "create_time": "2999-01-01T00:00:00Z"}], "next_page_token": None}

    api = _NoMatchApi(created_title="whatever")
    _patch(monkeypatch, api)
    for attempt in range(2):
        with pytest.raises(PermissionError, match=_DISABLED_CREATE):
            gc.create_genie_space(display_name="Ops_Collection_team_SQL_Agent",
                                  merged_config={"data_sources": {"tables": []}})
        _assert_no_legacy_activity(api)


def test_legacy_create_disabled_before_non_retrying_client_selection(monkeypatch):
    """Legacy create cannot POST, even with a non-retrying client available.

    The gated transport's non-retry guarantee is covered by test_genie_client.py::
    test_transport_disables_hidden_sdk_retries_for_post_and_both_patches.
    """
    title = "Client Selection Space"
    used = _FakeApiClient(created_title=title, created_id="sp_ok")
    base = _FakeApiClient(created_title="unused", created_id="sp_wrong")

    _patch(monkeypatch, base)
    monkeypatch.setattr(gc, "_non_retrying_client", Mock(return_value=_FakeClient(used)))

    def do(method, path, body=None, query=None):
        if method == "POST":
            used.post_calls += 1
            return {"space_id": "sp_ok"}
        raise AssertionError(method)
    used.do = Mock(side_effect=do)

    with pytest.raises(PermissionError, match=_DISABLED_CREATE):
        gc.create_genie_space(display_name=title, merged_config={"data_sources": {"tables": []}})
    _assert_no_legacy_activity(base)
    _assert_no_legacy_activity(used)
