from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts as vc
from backend.tests.vc_fakes.fixtures import (
    FakeCanonicalizer, actor_fixture, binding_fixture, executor_fixture,
)
from backend.tests.test_vc_drift import lookup, snapshot, version_id


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def observed_result(binding=None):
    binding = binding or binding_fixture()
    summary = vc.VersionSummary(version_id(2), binding.binding_id, NOW, vc.Origin.EXTERNAL,
                                "unknown", version_id(1), None, snapshot("external").fingerprints,
                                None, None)
    status = vc.BindingStatus(binding.binding_id, binding.binding_revision,
                              vc.Heads(version_id(2), version_id(1), version_id(1)),
                              vc.DriftState.EXTERNAL_AHEAD, False, None, NOW, NOW, False,
                              ("compare", "adopt", "reapply", "acknowledge"), ("Policy differs",))
    return vc.ObservationResult(status, summary, False)


def setup_service(**options):
    from backend.services.version_control.drift import DriftService

    observer = Mock(spec=vc.Observer)
    observer.capture.return_value = observed_result()
    service = DriftService(canonicalizer=FakeCanonicalizer(), observer=observer,
                           executor=executor_fixture(), **options)
    return service, observer


def test_capture_advances_observed_but_drift_remains_until_policy_resolution():
    service, observer = setup_service()
    captured = service.prepare_choice(binding_fixture())
    observer.capture.assert_called_once_with(binding_fixture(), "reconcile", executor_fixture())
    assert captured.status.heads == vc.Heads(version_id(2), version_id(1), version_id(1))
    result = service.classify(captured.status.heads,
                             lookup({version_id(1): snapshot("policy"),
                                     version_id(2): snapshot("external")}), "reachable")
    assert result.state == vc.DriftState.EXTERNAL_AHEAD
    assert captured.captured_version.version_id == version_id(2)


@pytest.mark.parametrize("failure", ["busy", "stale", "unresolved", "quarantined", "wrong_binding"])
def test_capture_failure_never_offers_reconciliation(failure):
    service, observer = setup_service()
    captured = observed_result()
    if failure == "busy":
        captured = replace(captured, busy=True)
    else:
        changes = {"stale": {"stale": True}, "unresolved": {"unresolved_operation_id": version_id(7)},
                   "quarantined": {"quarantined": True}, "wrong_binding": {"binding_id": version_id(99)}}
        captured = replace(captured, status=replace(captured.status, **changes[failure]))
    observer.capture.return_value = captured
    with pytest.raises(RuntimeError):
        service.prepare_choice(binding_fixture())
