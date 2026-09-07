"""M04 serialized observer port tests (no platform claims)."""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.observer import Observer
from backend.tests.test_vc_mutation_gate import rig, uid
from backend.tests.vc_fakes.fixtures import actor_fixture


@pytest.fixture
def observer_rig(rig):
    viewer = actor_fixture()
    status = vc.BindingStatus(rig.binding.binding_id, 1, vc.Heads(None, uid(), uid()),
                              vc.DriftState.EXTERNAL_AHEAD, False, None, None,
                              datetime.now(timezone.utc), False, (), ())
    identity = Mock(spec=vc.IdentityProvider)
    identity.executor.return_value = rig.executor
    selection = vc.ExplicitExecutorSelection(rig.executor.workspace_id, rig.executor.host,
        rig.executor.principal_id, rig.executor.execution_ref, None)
    rig.coordination.observe_exclusively.side_effect = lambda *args: rig.trace.append("lease") or vc.ObservationLease(
        rig.fence, 1, datetime.now(timezone.utc) + timedelta(minutes=1))
    rig.coordination.advance_heads.side_effect = lambda fence, update: rig.trace.append("advance") or vc.Heads(
        update.observed, status.heads.approved, status.heads.deployed)
    observer = Observer(coordination=rig.coordination, ledger=rig.ledger, canonicalizer=rig.canonicalizer,
        transport=rig.transport, identity=identity, reader_selection=selection,
        status_reader=lambda binding, actor: status)
    return rig, observer, viewer, status, identity


def test_capture_on_open_commits_before_display_and_advances_only_observed(observer_rig):
    rig, observer, viewer, status, identity = observer_rig
    result = observer.capture_on_open(rig.binding, viewer)
    assert result is not None, "Capture must return committed display evidence"
    assert rig.trace == ["lease", "get", "commit:open", "verify_commit", "advance"]
    assert result.captured_version.version_id == result.status.heads.observed
    assert result.status.heads.approved == status.heads.approved
    assert result.status.heads.deployed == status.heads.deployed
    assert result.status.drift != vc.DriftState.CLEAN
    update = rig.coordination.advance_heads.call_args.args[1]
    assert update.approved is update.deployed is None
    assert rig.transport.get.call_args.args[1] is rig.executor
    identity.executor.assert_called_once()


def test_concurrent_opens_deduplicate_without_regressing_or_mislabeling_managed_checkpoint(observer_rig):
    rig, observer, viewer, status, identity = observer_rig
    entered, release = Event(), Event()
    lease = rig.coordination.observe_exclusively.side_effect
    def acquire(*args):
        if entered.is_set():
            raise RuntimeError("active observation or managed checkpoint")
        return lease(*args)
    def slow_get(*args):
        entered.set()
        assert release.wait(5)
        return rig.get(*args)
    rig.coordination.observe_exclusively.side_effect = acquire
    rig.transport.get.side_effect = slow_get
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(observer.capture_on_open, rig.binding, viewer)
        assert entered.wait(5)
        try:
            second = observer.capture_on_open(rig.binding, viewer)
            assert second.busy and second.status.stale
            assert second.captured_version is None
            assert second.status.heads == status.heads
        finally:
            release.set()
        assert first.result().captured_version is not None
    rig.transport.get.assert_called_once()
    rig.ledger.append_observation.assert_called_once()


def test_unchanged_open_deduplicates_committed_observation(observer_rig):
    rig, observer, viewer, status, identity = observer_rig
    first = observer.capture_on_open(rig.binding, viewer)
    observer.status_reader = lambda *args: first.status
    second = observer.capture_on_open(rig.binding, viewer)
    assert second.status.heads.observed == first.status.heads.observed
    rig.ledger.append_observation.assert_called_once()
