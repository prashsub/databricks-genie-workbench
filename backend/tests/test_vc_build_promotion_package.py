"""Offline coverage for the shared promotion mapping/policy builder (D2.3/D2.5b).

`build_mapping_policy` is the single source of truth both the package builder
(D2.3) and the mint driver (D2.5b) derive their MappingSpec/TestPolicy from, so
the released request matches the pinned manifest. Here we pin determinism and the
mapping shape; the package assembly itself is covered by `test_vc_packages`.
"""

from backend.services.version_control import contracts as vc
from scripts.version_control.build_promotion_package import (
    build_mapping_policy,
    target_folder,
)

_CONFIG = {
    "target_binding": {
        "binding_id": "3955d52f-0f53-44dd-b559-325060e0ec6e", "binding_revision": 1,
        "space_key": "vc-promotion-gate", "workspace_id": "111",
        "space_id": "01f1ac63c82c18389837b663f442e409", "environment": "prod"},
    "source_table": "cat.src.orders", "target_table": "cat.tgt.orders",
    "target_warehouse_id": "wh-123",
}


def test_build_mapping_policy_is_deterministic_and_maps_source_to_target(monkeypatch):
    monkeypatch.setenv("VC_ADMIN_USER", "admin@example.com")
    mapping, policy = build_mapping_policy(_CONFIG)
    again, _ = build_mapping_policy(_CONFIG)
    assert isinstance(mapping, vc.MappingSpec) and isinstance(policy, vc.TestPolicy)
    # Deterministic: identical inputs -> identical content-addressed digest.
    assert mapping.mapping_digest == again.mapping_digest and len(mapping.mapping_digest) == 64
    assert mapping.mappings["cat.src.orders"] == "cat.tgt.orders"
    assert mapping.mappings["target:warehouse_id"] == "wh-123"
    assert mapping.mappings["target:parent_path"] == "/Users/admin@example.com"
    assert mapping.target_binding.binding_id == _CONFIG["target_binding"]["binding_id"]
    assert policy.thresholds == {"validation": {"smoke": True}, "benchmark": {"minimum": 1}}


def test_target_folder_falls_back_to_workspace_without_admin(monkeypatch):
    monkeypatch.delenv("VC_ADMIN_USER", raising=False)
    assert target_folder(_CONFIG) == "/Workspace"


def test_admin_user_change_changes_mapping_digest(monkeypatch):
    monkeypatch.setenv("VC_ADMIN_USER", "a@example.com")
    first, _ = build_mapping_policy(_CONFIG)
    monkeypatch.setenv("VC_ADMIN_USER", "b@example.com")
    second, _ = build_mapping_policy(_CONFIG)
    assert first.mapping_digest != second.mapping_digest
