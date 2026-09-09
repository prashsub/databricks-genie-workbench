"""`author_governed_config` transform (D2.5 bridge).

Proves the pure transform from the operator two-workspace config into the in-Job
governed schema produces a document that composes end-to-end through
`build_vc_runtime('promotion', config, adapters=Fake)` -> the real promotion
graph. This pins the authoring contract to `build_governed_seams`/`PlatformAdapters`
so the live phase only exercises credentials, never the config shape.
"""

import pytest

from backend.jobs import build_vc_runtime
from backend.services.version_control.platform.capabilities import (
    FIRST_WRITE_CAPABILITIES,
    topology_read_ready,
)
from backend.services.version_control.promotion import PromotionService
from backend.tests.test_vc_live_seams import FakeAdapters
from scripts.version_control.author_governed_config import author, to_job_config

TARGET_HOST = "https://target.example.com"
SOURCE_HOST = "https://source.example.com"
_DIRECTORY = {"host": TARGET_HOST, "workspace_id": "111", "principal_id": "directory-sp"}


def _two_workspace_config():
    return {
        "operation_id": "cc7a5ee2-9278-4e5c-b942-984e0a8c3238",
        "disposable_nonproduction": True,
        "roles": {
            "target": {"profile": "vc-primary-executor", "host": TARGET_HOST,
                       "workspace_id": "111", "principal_id": "exec-sp"},
            "source": {"profile": "vc-secondary-source", "host": SOURCE_HOST,
                       "workspace_id": "222", "principal_id": "source-sp"},
            "approval": {"profile": "vc-primary-approval", "host": TARGET_HOST,
                         "workspace_id": "111", "principal_id": "approval-sp"},
        },
        "volumes": {"outbound": "/Volumes/cat/vc_ctl/vc_outbound_packages",
                    "approval": "/Volumes/cat/vc_ctl/vc_approval_evidence",
                    "receipts": "/Volumes/cat/vc_ctl/vc_target_receipts"},
        "reverse_receipt_volume": "/Volumes/cat/vc_ctl/vc_target_receipts",
        "target_namespace": "cat.vc_ctl",
        "target_warehouse_id": "wh-abc",
        "target_binding": {"binding_id": "3955d52f-0f53-44dd-b559-325060e0ec6e",
                           "binding_revision": 1, "space_key": "vc-promotion-gate",
                           "workspace_id": "111", "space_id": "spaceTGT", "environment": "prod"},
        "source_space_id": "spaceSRC",
        "source_version_id": "015c812e-e090-401b-8c87-5e7a9b69fcdd",
        "manifest_uri": "/Volumes/cat/vc_ctl/vc_outbound_packages/sha256/deadbeef/manifest.json",
    }


def test_author_maps_roles_selections_and_proofs():
    governed = author(_two_workspace_config(), promotion_job_id=987)

    # Roles are re-keyed by runtime function and each carries the warehouse.
    assert set(governed["roles"]) == {"executor", "source", "approval"}
    assert governed["roles"]["executor"]["principal_id"] == "exec-sp"
    assert all(governed["roles"][r]["warehouse_id"] == "wh-abc" for r in governed["roles"])

    # Namespace split + executor selection pins the target-local Job.
    assert governed["catalog"] == "cat" and governed["control_schema"] == "vc_ctl"
    assert governed["target_selection"]["execution_ref"] == "job/987"
    assert governed["source_selection"]["execution_ref"].startswith("workbench")
    assert governed["promotion_job_id"] == 987

    # Source binding is derived + bound to the real source space, but lives in the
    # *target* workspace: the target-owned ledger only accepts versions in its
    # trusted workspace, so the source version is imported under a target-workspace
    # binding (the real source workspace is carried by source_workspace_id).
    assert governed["source_binding"]["space_id"] == "spaceSRC"
    assert governed["source_binding"]["workspace_id"] == "111"
    assert governed["source_workspace_id"] == "222"

    # The emitted proofs satisfy the live gates' read-only shape.
    assert topology_read_ready(governed["topology"]) is True
    assert all(governed["capabilities"][c] is True for c in FIRST_WRITE_CAPABILITIES)
    assert governed["flags"]["vc_promotion_enabled"] is True


def test_authored_config_composes_the_real_promotion_graph():
    governed = author(_two_workspace_config(), promotion_job_id=987)
    runtime = build_vc_runtime("promotion", governed, adapters=FakeAdapters())
    assert "promotion" in runtime._governed._handlers
    handler = runtime._governed._handlers["promotion"]
    service = next(cell.cell_contents for cell in handler.__closure__
                   if isinstance(cell.cell_contents, PromotionService))
    assert service.workspace_id == "111"
    assert service.outbound_volume == governed["volumes"]["outbound"]
    assert service.reverse_receipt_volume == governed["reverse_receipt_volume"]


def test_author_requires_promotion_job_id():
    with pytest.raises(ValueError, match="promotion_job_id required"):
        author(_two_workspace_config())


def test_to_job_config_adds_m2m_auth_and_dedicated_directory_role():
    """The in-Job config gives every role an m2m auth block (governed credential
    storage), keeps the profile as the identity routing key, and adds a dedicated
    directory role pinned via directory_role. The operator config is not mutated."""
    governed = author(_two_workspace_config(), promotion_job_id=987)
    job = to_job_config(governed, directory=_DIRECTORY)

    # Every runtime role now authenticates via injected M2M secrets, pinned to its
    # own host; the profile survives as the identity-provider routing key.
    for role in ("executor", "source", "approval"):
        auth = job["roles"][role]["auth"]
        assert auth["mode"] == "m2m"
        assert auth["host"] == governed["roles"][role]["host"]
        assert auth["client_id_env"] and auth["client_secret_env"]
        assert job["roles"][role]["profile"] == governed["roles"][role]["profile"]
    assert job["roles"]["executor"]["auth"]["client_id_env"] == "VC_EXECUTOR_CLIENT_ID"
    assert job["roles"]["source"]["auth"]["client_secret_env"] == "VC_SOURCE_CLIENT_SECRET"

    # Dedicated least-privileged directory reader, pinned for group resolution.
    directory = job["roles"]["directory"]
    assert directory["principal_id"] == "directory-sp"
    assert directory["auth"]["client_id_env"] == "VC_DIRECTORY_CLIENT_ID"
    assert "profile" not in directory  # never routed through executor()
    assert job["directory_role"] == "directory"

    # The operator config is untouched (deep copy).
    assert "auth" not in governed["roles"]["executor"]
    assert "directory" not in governed["roles"]


def test_job_config_composes_the_real_promotion_graph():
    """The augmented in-Job config still composes end-to-end through the real graph."""
    governed = author(_two_workspace_config(), promotion_job_id=987)
    job = to_job_config(governed, directory=_DIRECTORY)
    runtime = build_vc_runtime("promotion", job, adapters=FakeAdapters())
    assert "promotion" in runtime._governed._handlers


def test_author_reuses_existing_source_binding():
    cfg = _two_workspace_config()
    cfg["source_binding"] = {"binding_id": "fixed", "binding_revision": 1, "space_key": "k",
                             "workspace_id": "222", "space_id": "spaceSRC", "environment": "dev"}
    governed = author(cfg, promotion_job_id=1)
    assert governed["source_binding"]["binding_id"] == "fixed"
