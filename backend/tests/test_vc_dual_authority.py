from dataclasses import replace
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts as vc
from backend.tests.test_vc_reconcile import NOW, observed_result, scan_entry, scan_setup


def bundle_setup(entry=None, **options):
    from backend.services.version_control.drift.ports import BundleInventory, BundleResource, BundleSnapshot

    entry = entry or scan_entry(last_full_fetch_at=NOW)
    bundles = Mock(spec=BundleInventory)
    bundles.for_binding.return_value = BundleSnapshot((BundleResource(
        entry.binding.workspace_id, entry.binding.space_id, "sanctioned/deploy",
        "resources.genie_spaces.sales", True, None),), True)
    service, observer, inventory, projections = scan_setup([entry], bundles=bundles, **options)
    observer.capture.return_value = observed_result(entry.binding)
    return service, observer, inventory, projections, bundles, entry


def test_governed_bundle_content_is_flagged_within_one_cycle():
    service, observer, inventory, projections, bundles, entry = bundle_setup()
    batch = service.scan("123", None)
    observer.capture.assert_called_once()
    bundles.for_binding.assert_called_once_with(entry.binding)
    status = batch.items[0]
    assert status.drift == vc.DriftState.CONFLICTED
    assert status.heads == observed_result(entry.binding).status.heads
    assert status.allowed_actions == ()
    assert not status.quarantined
    assert "resources.genie_spaces.sales" in " ".join(status.reasons)
    assert "actor=unknown" in " ".join(status.reasons)
    assert "sanctioned/deploy" in " ".join(status.reasons)
    assert projections.publish.call_args.args[1] == status
    assert entry.governed_by == "workbench"


@pytest.mark.parametrize("case", ["other_workspace", "other_space", "infrastructure", "ungoverned"])
def test_bundle_detection_joins_physical_identity_and_content_ownership(case):
    from backend.services.version_control.drift.ports import BundleSnapshot

    entry = scan_entry(governed_by="external") if case == "ungoverned" else scan_entry()
    service, observer, inventory, projections, bundles, entry = bundle_setup(entry)
    resource = bundles.for_binding.return_value.resources[0]
    changes = {"other_workspace": {"workspace_id": "other"}, "other_space": {"space_id": "other"},
               "infrastructure": {"manages_content": False}, "ungoverned": {}}
    bundles.for_binding.return_value = BundleSnapshot((replace(resource, **changes[case]),), True)
    assert service.scan("123", None).items[0].drift != vc.DriftState.CONFLICTED


@pytest.mark.parametrize("failure", ["incomplete", "unavailable", "capture"])
def test_bundle_uncertainty_is_not_clear_or_inferred_actor(failure):
    service, observer, inventory, projections, bundles, entry = bundle_setup()
    if failure == "incomplete":
        bundles.for_binding.return_value = replace(bundles.for_binding.return_value, complete=False)
    if failure == "unavailable":
        bundles.for_binding.side_effect = RuntimeError("unavailable")
    if failure == "capture":
        observer.capture.side_effect = RuntimeError("cannot observe")
    status = service.scan("123", None).items[0]
    assert status.drift == (vc.DriftState.CONFLICTED if failure == "capture" else vc.DriftState.UNKNOWN)
    assert status.allowed_actions == ()
    assert status.stale
