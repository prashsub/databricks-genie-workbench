"""Reverse-receipt mirror: the target publishes each confirmed receipt to a
source-readable volume so the source workspace can poll its outcome without any
target write authority (M07 cross-workspace receipt-back)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.promotion import receipts
from backend.services.version_control.promotion.packages import digest, encode
from backend.tests.test_vc_packages import MemoryFiles, uid


def _receipt():
    d = "a" * 64
    binding = vc.BindingRef(uid(2), 1, "sales", "target", "target-space", "prod")
    fp = vc.Fingerprints(d, d, d, "vc-c14n/1")
    return vc.DeploymentReceipt(
        schema_version="VC/1.0", release_id=uid(6), operation_id=uid(4), target_binding=binding,
        attempt_id=uid(7), generation=1, approval_id=uid(4), approval_digest=d,
        source_version_id=uid(3), package_digest=d, mapping_digest=d, intended_fingerprints=fp,
        rendered_fingerprints=fp, observed_fingerprints=fp, pre_version_id=uid(10),
        post_version_id=uid(11), transformer_version="vc-map/1", canonicalizer_version="vc-c14n/1",
        executor_id="target-sp", job_run_id="job/1", validation_evidence_digest=d,
        benchmark_evidence_digest=d, status=vc.OperationStatus.CONFIRMED,
        compensation_operation_id=None, recorded_at=datetime.now(UTC))


def _service(**overrides):
    service = SimpleNamespace(
        receipt_store=MemoryFiles(),
        receipt_volume="/Volumes/control/vc/vc_target_receipts",
        reverse_receipt_volume=None)
    service.__dict__.update(overrides)
    return service


def test_export_without_reverse_volume_writes_only_forward():
    service = _service()
    receipt = _receipt()
    receipts.export(service, receipt)
    suffix = "/sha256/" + digest(receipt) + "/receipt.json"
    assert service.receipt_store.read(service.receipt_volume + suffix) == encode(receipt)
    assert not any(path.startswith("/Volumes/control/vc/vc_reverse")
                   for path in service.receipt_store.files)


def test_export_mirrors_receipt_to_reverse_volume_source_readable():
    reverse = "/Volumes/control/vc/vc_reverse_receipts"
    service = _service(reverse_receipt_volume=reverse)
    receipt = _receipt()
    receipts.export(service, receipt)
    suffix = "/sha256/" + digest(receipt) + "/receipt.json"
    # Forward (target-owned) and reverse (source-readable) copies are byte-identical.
    assert service.receipt_store.read(service.receipt_volume + suffix) == encode(receipt)
    assert service.receipt_store.read(reverse + suffix) == encode(receipt)


def test_reverse_mirror_is_idempotent_and_immutable():
    reverse = "/Volumes/control/vc/vc_reverse_receipts"
    service = _service(reverse_receipt_volume=reverse)
    receipt = _receipt()
    receipts.export(service, receipt)
    # Re-export is a no-op on identical bytes (content-addressed immutable put).
    receipts.export(service, receipt)
    suffix = "/sha256/" + digest(receipt) + "/receipt.json"
    assert service.receipt_store.read(reverse + suffix) == encode(receipt)


def test_reverse_mirror_failure_propagates_so_export_retries():
    reverse = "/Volumes/control/vc/vc_reverse_receipts"
    service = _service(reverse_receipt_volume=reverse)
    receipt = _receipt()
    forward_prefix = service.receipt_volume + "/sha256/" + digest(receipt)
    reverse_prefix = reverse + "/sha256/" + digest(receipt)

    original = service.receipt_store.put_if_absent
    calls = {"n": 0}

    def flaky(path, content):
        # Fail the first reverse write; the forward copy already succeeded.
        if path.startswith(reverse_prefix):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("reverse share unavailable")
        original(path, content)

    service.receipt_store.put_if_absent = flaky
    with pytest.raises(OSError):
        receipts.export(service, receipt)
    # Forward copy is durable; a retry (immutable put) republishes the reverse copy
    # without rewriting the forward one, and returns the receipt.
    service.receipt_store.put_if_absent = original
    assert receipts.export(service, receipt) == receipt
    assert service.receipt_store.read(forward_prefix + "/receipt.json") == encode(receipt)
    assert service.receipt_store.read(reverse_prefix + "/receipt.json") == encode(receipt)
