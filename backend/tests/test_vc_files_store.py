"""Offline contract tests for the Files-backed content-addressed store."""

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.promotion.packages import immutable_put
from backend.services.version_control.promotion.store import FilesVolumeStore


def _dict_store():
    """A FilesVolumeStore over an in-memory dict with SDK-shaped seams
    (exists-check + overwrite=False put), so the immutable-put contract is
    exercised without a real Volume."""
    blobs: dict[str, bytes] = {}

    def read(path):
        if path not in blobs:
            raise FileNotFoundError(path)
        return blobs[path]

    def put(path, content):
        # Mirrors files.upload(overwrite=False): a second write is a hard error.
        if path in blobs:
            raise AssertionError("overwrite=False must never be called on an existing path")
        blobs[path] = content

    def exists(path):
        return path in blobs

    published: list = []
    return FilesVolumeStore(read=read, put=put, exists=exists,
                            publish=published.append), blobs, published


def test_read_returns_raw_bytes():
    store, blobs, _ = _dict_store()
    blobs["/Volumes/c/s/v/a"] = b"payload"
    assert store.read("/Volumes/c/s/v/a") == b"payload"


def test_put_if_absent_writes_once():
    store, blobs, _ = _dict_store()
    store.put_if_absent("/Volumes/c/s/v/a", b"payload")
    assert blobs["/Volumes/c/s/v/a"] == b"payload"


def test_put_if_absent_is_idempotent_and_never_overwrites():
    store, blobs, _ = _dict_store()
    store.put_if_absent("/Volumes/c/s/v/a", b"payload")
    # Second call with identical bytes must not attempt an overwrite=False upload.
    store.put_if_absent("/Volumes/c/s/v/a", b"payload")
    assert blobs["/Volumes/c/s/v/a"] == b"payload"


def test_immutable_put_roundtrip_matches_bytes():
    store, _, _ = _dict_store()
    immutable_put(store, "/Volumes/c/s/v/manifest.json", b"{}")
    assert store.read("/Volumes/c/s/v/manifest.json") == b"{}"


def test_immutable_put_detects_content_collision():
    store, blobs, _ = _dict_store()
    blobs["/Volumes/c/s/v/x"] = b"original"
    # An existing path with different bytes must fail closed (collision), never
    # silently accept a divergent artifact at a content-addressed location.
    with pytest.raises(ValueError):
        immutable_put(store, "/Volumes/c/s/v/x", b"different")


def test_publish_forwards_reference_and_returns_it():
    store, _, published = _dict_store()
    reference = vc.PackageRef("d" * 64, "/Volumes/c/s/v/sha256/x/manifest.json")
    assert store.publish(reference) is reference
    assert published == [reference]


def test_publish_is_optional():
    store = FilesVolumeStore(read=lambda p: b"", put=lambda p, c: None, exists=lambda p: False)
    reference = vc.PackageRef("d" * 64, "/Volumes/c/s/v/sha256/x/manifest.json")
    assert store.publish(reference) is reference
