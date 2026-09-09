"""Offline composition test for the live governed promotion graph.

Injects a fake low-level `adapters` object (SQL/Volume/SDK/identity seams) into
`build_vc_runtime("promotion", config, adapters=...)` and asserts the *real* leaf
constructors assemble into a working governed runtime: the promotion handler is
reachable and the promotion service holds the real gate/transport/observer graph.

No live platform: only the lowest-level clients are faked. This pins the wiring
(`build_governed_seams` -> `resolve_governed_ports` -> `assemble_vc_runtime`) so
the live integration step exercises credentials, not composition.
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.jobs import build_vc_runtime
from backend.services.version_control import contracts as vc
from backend.services.version_control.mutation_gate import MutationGate
from backend.services.version_control.promotion import PromotionService
from backend.services.version_control.promotion.transport import NativeTransport
from backend.tests.test_vc_packages import MemoryFiles


def uid(n):
    return str(UUID(int=n))


TARGET_HOST = "https://target.example.com"
SOURCE_HOST = "https://source.example.com"


def _executor(spec):
    return vc.ExecutorContext(spec["workspace_id"], spec["host"], spec["principal_id"],
                              "service", object(), spec["execution_ref"])


class FakeAdapters:
    """Low-level seam surface used by `build_governed_seams`, all faked."""

    def __init__(self):
        self.stores = {}

    def sql(self, _role):
        return lambda statement, parameters=None: []

    def files_store(self, role):
        return self.stores.setdefault(role, MemoryFiles())

    def read_evidence(self, _uri):
        return b""

    def authenticate(self, _executor):
        return {"Authorization": "Bearer test"}

    def identity_provider(self, role):
        host = TARGET_HOST if role != "source" else SOURCE_HOST
        ws = "target" if role != "source" else "source"
        return SimpleNamespace(
            executor=lambda selection: _executor(
                {"workspace_id": ws, "host": host,
                 "principal_id": f"{role}-sp", "execution_ref": "job/123"}),
            actor=lambda request: vc.ActorContext(request.authentication_reference, ws, "service"))

    def capability_probe(self):
        return lambda: {"ok": True}

    def topology_proof(self):
        return {"source_workspace": "source", "target_workspace": "target",
                "source_metastore": "meta-1", "target_metastore": "meta-1",
                "remote_write_credentials": False, "packages_readable": True}

    def attempt_inventory(self, *_a):
        return None

    def job_reader(self, *_a):
        return None

    def worker_supervisor_reader(self, *_a):
        return None

    def dependency_checker(self):
        return SimpleNamespace(check=lambda *a, **k: True)

    def release_facts_reader(self, _facts):
        return lambda operation_id: []

    def pending_reader(self, _facts):
        return lambda workspace_id, cursor: vc.Page((), None)

    def dispatcher(self):
        return SimpleNamespace(
            submit_local=lambda operation_id, kind: vc.OperationHandle(
                operation_id, vc.OperationStatus.REQUESTED, "run-1"))


def _config():
    target_selection = {"workspace_id": "target", "host": TARGET_HOST,
                        "principal_id": "target-sp", "execution_ref": "job/123",
                        "profile": "target-profile"}
    source_selection = {"workspace_id": "source", "host": SOURCE_HOST,
                        "principal_id": "source-sp", "execution_ref": "job/1",
                        "profile": "source-profile"}
    source_binding = vc.to_wire(
        vc.BindingRef(uid(1), 1, "sales", "source", "source-space", "dev"))
    return {
        "workspace_id": "target",
        "catalog": "sandbox_cat",
        "control_schema": "vc_ctl",
        "warehouse_id": "wh-123",
        "target_warehouse_id": "wh-123",
        "principals": {"runtime": "target-sp"},
        "flags": {"vc_writes_enabled": True, "vc_promotion_enabled": True},
        "target_host": TARGET_HOST,
        "source_workspace_id": "source",
        "target_selection": target_selection,
        "source_selection": source_selection,
        "source_binding": source_binding,
        "volumes": {"outbound": "/Volumes/sandbox_cat/vc_ctl/vc_outbound_packages",
                    "approval": "/Volumes/sandbox_cat/vc_ctl/vc_approval_evidence",
                    "receipts": "/Volumes/sandbox_cat/vc_ctl/vc_target_receipts"},
        "reverse_receipt_volume": "/Volumes/sandbox_cat/vc_ctl/vc_reverse_receipts",
        "promotion_job_id": 42,
    }


def test_promotion_graph_assembles_with_real_leaves():
    runtime = build_vc_runtime("promotion", _config(), adapters=FakeAdapters())
    # The governed runtime exposes the durable ports and a promotion handler.
    assert runtime.workspace_id == "target"
    assert runtime.facts is not None and runtime.gate is not None
    assert isinstance(runtime.gate, MutationGate)
    # The promotion handler is wired (restore/optimizer/reconcile are not).
    assert "promotion" in runtime._governed._handlers
    for absent in ("restore", "optimizer_apply", "reconcile"):
        assert absent not in runtime._governed._handlers


def test_promotion_service_holds_verifier_transport_and_reverse_volume():
    seams_config = _config()
    runtime = build_vc_runtime("promotion", seams_config, adapters=FakeAdapters())
    handler = runtime._governed._handlers["promotion"]
    # Reach into the closure to inspect the constructed PromotionService.
    service = next(cell.cell_contents for cell in handler.__closure__
                   if isinstance(cell.cell_contents, PromotionService))
    assert isinstance(service, PromotionService)
    # Promotion service transport is the topology *verifier*, not the gate's
    # Genie write transport.
    assert isinstance(service.transport, NativeTransport)
    assert service.reverse_receipt_volume == seams_config["reverse_receipt_volume"]
    assert service.outbound_volume == seams_config["volumes"]["outbound"]
    assert service.receipt_volume == seams_config["volumes"]["receipts"]
    assert service.workspace_id == "target"
    assert service.target_host == TARGET_HOST


def test_incomplete_config_stays_fail_closed():
    broken = {k: v for k, v in _config().items() if k != "catalog"}
    with pytest.raises(PermissionError, match="not integrated"):
        build_vc_runtime("promotion", broken, adapters=FakeAdapters())


def test_rows_coerces_statement_api_strings_to_store_native_types():
    """The REST Statements API returns every cell as a string; `_rows` must hand the
    durable stores typed fences/booleans/timestamps like the typed connector."""
    from datetime import datetime

    from backend.services.version_control.platform.live_seams import _rows

    response = {
        "manifest": {"schema": {"columns": [
            {"name": "binding_revision", "type_name": "LONG"},
            {"name": "state", "type_name": "STRING"},
            {"name": "unresolved", "type_name": "BOOLEAN"},
            {"name": "updated_at", "type_name": "TIMESTAMP"},
            {"name": "observed_sequence", "type_name": "INT"},
        ]}},
        "result": {"data_array": [
            ["1", "idle", "false", "2026-09-09 19:30:39.233", None],
        ]},
    }
    (row,) = _rows(response)
    assert row["binding_revision"] == 1 and isinstance(row["binding_revision"], int)
    assert row["state"] == "idle"
    assert row["unresolved"] is False
    # Naive UTC: `row_from_columns` reattaches tz via `_from_naive_utc`.
    assert row["updated_at"] == datetime(2026, 9, 9, 19, 30, 39, 233000)  # noqa: DTZ001
    assert row["observed_sequence"] is None


def test_clock_satisfies_both_callable_and_now_contracts():
    """One `_Clock` backs the whole governed graph: CoordinationService calls
    `clock.now()`; the durable stores (DeltaCoordinationStore/DeltaRegistry) and
    DriftService call `clock()`. Both must return an aware UTC datetime."""
    from datetime import timezone

    from backend.services.version_control.platform.live_seams import _Clock

    clock = _Clock()
    assert callable(clock)
    called, vianow = clock(), clock.now()
    assert called.tzinfo == timezone.utc and vianow.tzinfo == timezone.utc
