"""M10: production IsolationRegistry + create/own/tear-down candidate lifecycle.

These are offline, fake-backed unit tests for the concrete isolation registry
and the candidate-space lifecycle context manager. They intentionally do NOT
flip the job-notebook `require_candidate_session_for_job()` refusals: that swap
is platform-gated (M08 target-local composition + real lifecycle validation)
per docs/design/version_control M10. The lifecycle here is fail-closed by
default and must guarantee teardown (ownership revoke + candidate delete) even
when the session body raises.
"""

from unittest.mock import Mock

import pytest


class FakeTransport:
    """Deterministic candidate create/delete transport with call recording."""

    def __init__(self):
        self.created = []
        self.deleted = []
        self.fail_delete = False

    def create_candidate(self, workspace_id, source_space_id, run_id):
        candidate_id = f"cand-{run_id}"
        self.created.append((workspace_id, source_space_id, run_id, candidate_id))
        return candidate_id

    def delete_candidate(self, workspace_id, space_id):
        self.deleted.append((workspace_id, space_id))
        if self.fail_delete:
            raise RuntimeError("delete failed")


def _managed_lookup(managed):
    """Return a resolve_physical-style callable backed by a managed-binding set."""

    def lookup(workspace_id, space_id):
        return object() if (workspace_id, space_id) in managed else None

    return lookup


# ---------------------------------------------------------------------------
# OptimizerIsolationRegistry
# ---------------------------------------------------------------------------


def test_registry_reports_managed_binding_via_injected_lookup():
    from genie_space_optimizer.integration.version_control import (
        OptimizerIsolationRegistry,
    )

    managed = {("ws", "live")}
    registry = OptimizerIsolationRegistry(_managed_lookup(managed), lambda client: "ws")
    assert registry.resolve_physical("ws", "live") is not None
    assert registry.resolve_physical("ws", "cand-run") is None
    assert registry.workspace_id_for(Mock()) == "ws"


def test_registry_ownership_is_claimed_and_released():
    from genie_space_optimizer.integration.version_control import (
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup(set()), lambda client: "ws")
    assert registry.optimizer_owner("ws", "cand") is None
    registry.claim("ws", "cand", "run")
    assert registry.optimizer_owner("ws", "cand") == "run"
    registry.release("ws", "cand", "run")
    assert registry.optimizer_owner("ws", "cand") is None


def test_registry_refuses_conflicting_owner():
    from genie_space_optimizer.integration.version_control import (
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup(set()), lambda client: "ws")
    registry.claim("ws", "cand", "runA")
    with pytest.raises(PermissionError, match="already owned"):
        registry.claim("ws", "cand", "runB")
    assert registry.optimizer_owner("ws", "cand") == "runA"


def test_registry_refuses_claiming_managed_binding():
    from genie_space_optimizer.integration.version_control import (
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup({("ws", "live")}), lambda client: "ws")
    with pytest.raises(PermissionError, match="managed binding"):
        registry.claim("ws", "live", "run")
    assert registry.optimizer_owner("ws", "live") is None


def test_registry_release_requires_owner():
    from genie_space_optimizer.integration.version_control import (
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup(set()), lambda client: "ws")
    registry.claim("ws", "cand", "runA")
    with pytest.raises(PermissionError, match="not owned"):
        registry.release("ws", "cand", "runB")
    assert registry.optimizer_owner("ws", "cand") == "runA"


# ---------------------------------------------------------------------------
# CandidateSpaceLifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_writes_default_off():
    from genie_space_optimizer.integration.version_control import (
        CandidateSpaceLifecycle,
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup({("ws", "live")}), lambda client: "ws")
    transport = FakeTransport()
    lifecycle = CandidateSpaceLifecycle(registry, transport)  # writes_enabled defaults off
    with pytest.raises(PermissionError, match="disabled"), lifecycle.session("run", "ws", "live", Mock()):
        pass
    assert transport.created == []
    assert transport.deleted == []


def test_lifecycle_creates_owns_and_tears_down():
    from genie_space_optimizer.integration.version_control import (
        CandidateSpaceLifecycle,
        OptimizerIsolationRegistry,
        _EvaluationClient,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup({("ws", "live")}), lambda client: "ws")
    transport = FakeTransport()
    lifecycle = CandidateSpaceLifecycle(registry, transport, writes_enabled=True)
    with lifecycle.session("run", "ws", "live", Mock()) as session:
        assert isinstance(session.client, _EvaluationClient)
        assert session.resource.space_id == "cand-run"
        assert registry.optimizer_owner("ws", "cand-run") == "run"
    assert transport.created and transport.created[0][3] == "cand-run"
    assert transport.deleted == [("ws", "cand-run")]
    assert registry.optimizer_owner("ws", "cand-run") is None


def test_lifecycle_tears_down_on_body_exception():
    from genie_space_optimizer.integration.version_control import (
        CandidateSpaceLifecycle,
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup({("ws", "live")}), lambda client: "ws")
    transport = FakeTransport()
    lifecycle = CandidateSpaceLifecycle(registry, transport, writes_enabled=True)
    with pytest.raises(ValueError, match="boom"), lifecycle.session("run", "ws", "live", Mock()):
        raise ValueError("boom")
    assert transport.deleted == [("ws", "cand-run")]
    assert registry.optimizer_owner("ws", "cand-run") is None


def test_lifecycle_refuses_non_managed_source():
    from genie_space_optimizer.integration.version_control import (
        CandidateSpaceLifecycle,
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup(set()), lambda client: "ws")
    transport = FakeTransport()
    lifecycle = CandidateSpaceLifecycle(registry, transport, writes_enabled=True)
    with pytest.raises(PermissionError, match="managed live space"), lifecycle.session("run", "ws", "live", Mock()):
        pass
    assert transport.created == []


def test_lifecycle_deletes_candidate_if_claim_fails():
    from genie_space_optimizer.integration.version_control import (
        CandidateSpaceLifecycle,
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup({("ws", "live")}), lambda client: "ws")
    registry.claim("ws", "cand-run", "other")  # candidate id already owned by another run
    transport = FakeTransport()
    lifecycle = CandidateSpaceLifecycle(registry, transport, writes_enabled=True)
    with pytest.raises(PermissionError, match="already owned"), lifecycle.session("run", "ws", "live", Mock()):
        pass
    # the just-created candidate must be cleaned up; foreign ownership untouched
    assert transport.deleted == [("ws", "cand-run")]
    assert registry.optimizer_owner("ws", "cand-run") == "other"


def test_lifecycle_session_targets_only_candidate():
    from genie_space_optimizer.integration.version_control import (
        CandidateSpaceLifecycle,
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup({("ws", "live")}), lambda client: "ws")
    transport = FakeTransport()
    lifecycle = CandidateSpaceLifecycle(registry, transport, writes_enabled=True)
    inner = Mock()
    with lifecycle.session("run", "ws", "live", inner) as session:
        session.client.api_client.do("PATCH", "/api/2.0/genie/spaces/cand-run")
        with pytest.raises(PermissionError):
            session.client.api_client.do("PATCH", "/api/2.0/genie/spaces/live")
    assert inner.api_client.do.call_count == 1


def test_lifecycle_revokes_ownership_even_when_delete_fails():
    from genie_space_optimizer.integration.version_control import (
        CandidateSpaceLifecycle,
        OptimizerIsolationRegistry,
    )

    registry = OptimizerIsolationRegistry(_managed_lookup({("ws", "live")}), lambda client: "ws")
    transport = FakeTransport()
    transport.fail_delete = True
    lifecycle = CandidateSpaceLifecycle(registry, transport, writes_enabled=True)
    with pytest.raises(RuntimeError, match="delete failed"), lifecycle.session("run", "ws", "live", Mock()):
        pass
    # a failed delete must still revoke ownership so the leaked id cannot be reused silently
    assert registry.optimizer_owner("ws", "cand-run") is None
