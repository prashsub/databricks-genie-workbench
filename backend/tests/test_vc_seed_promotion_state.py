"""Offline tests for the D2.5a durable promotion-state seed.

The seed drives the *real* M02/M03 write paths (`DeltaRegistry` /
`DeltaCoordinationStore` / `DeltaVersionLedger`) to bring the target binding to
BOUND, create the single IDLE coordination row, and import the pinned source
version into the target-owned ledger. These tests pin (1) the idempotent
orchestration against mock stores, (2) the drift guard `verify_source` against a
real package manifest, and (3) that `build_seed_stores` composes the genuine
adapters with the id generators and initializer the live run relies on.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from backend.services.config_fingerprint import Canonicalizer
from backend.services.version_control import contracts as vc
from backend.services.version_control.coordination.store import DeltaCoordinationStore
from backend.services.version_control.ledger import DeltaVersionLedger
from backend.services.version_control.promotion import packages
from backend.services.version_control.registry import DeltaRegistry
from backend.tests.test_vc_live_seams import FakeAdapters, _config
from scripts.version_control.seed_promotion_state import (
    build_seed_stores,
    seed,
    verify_source,
)

TARGET_WS = "target"
SOURCE_VERSION_ID = str(UUID(int=7))


def _target_binding():
    return vc.BindingRef(str(UUID(int=2)), 1, "vc-promotion-gate", TARGET_WS, "target-space", "prod")


def _source_binding():
    # Lives in the *target* workspace (ledger trust); space_id is the source space.
    return vc.BindingRef(str(UUID(int=1)), 1, "vc-promotion-gate-source", TARGET_WS,
                         "source-space", "dev")


def _package():
    """A real snapshot + manifest so `verify_source` runs the true digest checks."""
    canon = Canonicalizer()
    envelope = {"space_id": "source-space", "workspace_id": "source-ws",
                "warehouse_id": "src-wh", "parent_path": "/src",
                "serialized_space": {"version": 2,
                                     "data_sources": {"tables": [{"identifier": "dev.s.orders"}]}},
                "description": "Sales"}
    snapshot = canon.observe(envelope)
    version = vc.Version(SOURCE_VERSION_ID, snapshot, vc.CaptureContext(
        _source_binding(), "capture", datetime.now(UTC), "initial",
        vc.ActorContext("exec-sp", TARGET_WS, "service"), vc.Origin.WORKBENCH))
    mapping = vc.MappingSpec("VC/1.0", _target_binding(), {"dev.s.orders": "prod.s.orders"},
                             "0" * 64, "vc-map/1")
    policy = vc.TestPolicy("VC/1.0", packages.digest({"smoke": True}),
                           packages.digest({"minimum": 1}),
                           {"validation": {"smoke": True}, "benchmark": {"minimum": 1}})
    manifest, _files = packages.build(version, mapping, policy)
    return canon, envelope, snapshot, manifest


def _actor():
    return vc.ActorContext("exec-sp", TARGET_WS, "service")


def _mock_stores(*, resolve_side_effect=None, resolve_return=None, coord_state=vc.CoordinationState.IDLE):
    stores = SimpleNamespace(registry=Mock(), coordination=Mock(), ledger=Mock())
    if resolve_side_effect is not None:
        stores.registry.resolve.side_effect = resolve_side_effect
    else:
        stores.registry.resolve.return_value = resolve_return
    stores.registry.bind_created.return_value = _target_binding()
    stores.coordination.read.return_value = SimpleNamespace(state=coord_state)
    stores.ledger.append_observation.return_value = vc.ObservationRef(
        SOURCE_VERSION_ID, _source_binding().binding_id, 1, "a" * 64, "b" * 64)
    return stores


def _run_seed(stores, canon, envelope, manifest):
    return seed(stores, target_binding=_target_binding(), source_binding=_source_binding(),
                capture_envelope=lambda: envelope, canonicalizer=canon, manifest=manifest,
                actor=_actor(), now=datetime.now(UTC))


# -- verify_source --------------------------------------------------------

def test_verify_source_passes_for_the_pinned_snapshot():
    _canon, _envelope, snapshot, manifest = _package()
    verify_source(snapshot, manifest)  # no raise


def test_verify_source_rejects_a_drifted_source():
    canon, _envelope, _snapshot, manifest = _package()
    drifted = canon.observe({"space_id": "source-space", "serialized_space":
                             {"version": 2, "data_sources": {"tables": [{"identifier": "dev.s.CHANGED"}]}},
                             "description": "Sales"})
    with pytest.raises(ValueError, match="rebuild the promotion package"):
        verify_source(drifted, manifest)


# -- seed orchestration ---------------------------------------------------

def test_seed_fresh_binding_enrolls_binds_and_imports():
    canon, envelope, _snapshot, manifest = _package()
    stores = _mock_stores(resolve_side_effect=KeyError("no events"))
    result = _run_seed(stores, canon, envelope, manifest)

    assert result["registry"] == "enrolled+bound"
    assert result["coordination_state"] == "idle"
    assert result["source_version_id"] == SOURCE_VERSION_ID
    stores.registry.enroll.assert_called_once()
    stores.registry.bind_created.assert_called_once()
    stores.ledger.append_observation.assert_called_once()
    # The imported version carries the target-workspace source binding.
    _snapshot_arg, context = stores.ledger.append_observation.call_args.args
    assert context.binding == _source_binding()
    assert context.actor.workspace_id == TARGET_WS


def test_seed_is_idempotent_when_already_bound():
    canon, envelope, _snapshot, manifest = _package()
    stores = _mock_stores(resolve_return=_target_binding())
    result = _run_seed(stores, canon, envelope, manifest)

    assert result["registry"] == "already-bound"
    stores.registry.enroll.assert_not_called()
    stores.registry.bind_created.assert_not_called()
    # The source version import is still (idempotently) attempted.
    stores.ledger.append_observation.assert_called_once()


def test_seed_binds_a_provisional_head_without_re_enrolling():
    canon, envelope, _snapshot, manifest = _package()
    provisional = vc.BindingRef(_target_binding().binding_id, 1, "vc-promotion-gate",
                                TARGET_WS, None, "prod")
    stores = _mock_stores(resolve_return=provisional)
    result = _run_seed(stores, canon, envelope, manifest)

    assert result["registry"] == "bound"
    stores.registry.enroll.assert_not_called()
    stores.registry.bind_created.assert_called_once()


def test_seed_fails_closed_when_coordination_row_missing():
    canon, envelope, _snapshot, manifest = _package()
    stores = _mock_stores(resolve_side_effect=KeyError("no events"))
    stores.coordination.read.return_value = None
    with pytest.raises(ValueError, match="Coordination row missing"):
        _run_seed(stores, canon, envelope, manifest)


def test_seed_rejects_ledger_version_id_disagreement():
    canon, envelope, _snapshot, manifest = _package()
    stores = _mock_stores(resolve_side_effect=KeyError("no events"))
    stores.ledger.append_observation.return_value = vc.ObservationRef(
        str(UUID(int=99)), _source_binding().binding_id, 1, "a" * 64, "b" * 64)
    with pytest.raises(ValueError, match="!= manifest"):
        _run_seed(stores, canon, envelope, manifest)


# -- build_seed_stores composition ---------------------------------------

def test_build_seed_stores_wires_real_adapters_with_pinned_generators():
    config = _config()  # catalog/control_schema/workspace_id = target namespace
    target_binding = _target_binding()
    stores = build_seed_stores(config, target_binding=target_binding,
                               source_version_id=SOURCE_VERSION_ID, adapters=FakeAdapters())

    assert isinstance(stores.registry, DeltaRegistry)
    assert isinstance(stores.coordination, DeltaCoordinationStore)
    assert isinstance(stores.ledger, DeltaVersionLedger)
    # Injected id generators pin the configured identities.
    assert stores.registry.new_binding_id() == target_binding.binding_id
    assert stores.ledger.new_version_id() == SOURCE_VERSION_ID
    # Enrollment initialization routes to the coordination store's IDLE-row insert.
    assert stores.registry.initialize == stores.coordination.initialize
    # Both fact writers are write-enabled and bound to the trusted target workspace.
    assert stores.registry.writes_enabled and stores.ledger.writes_enabled
    assert stores.registry.workspace_id == config["workspace_id"]
    assert stores.ledger.target_workspace_id == config["workspace_id"]
