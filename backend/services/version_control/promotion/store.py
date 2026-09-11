"""Content-addressed store over Unity Catalog Volume Files.

`PromotionService` reads package/receipt bytes and writes immutable artifacts
through a small store port (`read` / `put_if_absent` / `publish`). The fake used
offline is `MemoryFiles`; this is the live variant. It is deliberately built
from injected seams (a `read`, a `put`, an `exists` check and an optional
`publish`) rather than reaching for a `WorkspaceClient` directly, so the
immutable-put contract is offline-testable and the credential-bound SDK calls
live only in the seam factory that constructs it.

Immutability is enforced two ways: `put_if_absent` skips the write when the path
already exists (so retries never attempt an `overwrite=False` upload that the
Files API would reject), and `packages.immutable_put` re-reads and byte-compares
after every put, turning any content-addressed collision into a hard error.
"""

from collections.abc import Callable
from typing import Any


class FilesVolumeStore:
    def __init__(self, *, read: Callable[[str], bytes], put: Callable[[str, bytes], Any],
                 exists: Callable[[str], bool], publish: Callable[[Any], Any] | None = None):
        self._read = read
        self._put = put
        self._exists = exists
        self._publish = publish

    def read(self, path: str) -> bytes:
        return self._read(path)

    def put_if_absent(self, path: str, content: bytes) -> None:
        # Content-addressed paths are write-once. If the artifact is already
        # present we leave it untouched; the caller's read-back comparison
        # (immutable_put) still verifies the bytes match, so a divergent object
        # at the same path fails closed instead of being silently overwritten.
        if self._exists(path):
            return
        self._put(path, content)

    def publish(self, reference: Any) -> Any:
        if self._publish is not None:
            self._publish(reference)
        return reference


def files_volume_store(client, *, publish: Callable[[Any], Any] | None = None) -> FilesVolumeStore:
    """Bind a `FilesVolumeStore` to a credentialed `WorkspaceClient`.

    `read` streams the Volume file, `put` uploads with `overwrite=False` (the
    Files API rejects clobbering an existing object), and `exists` probes
    metadata, treating a not-found as absent and re-raising every other error so
    a transient outage never masquerades as "safe to overwrite".
    """

    from io import BytesIO

    from databricks.sdk.errors import NotFound

    def read(path: str) -> bytes:
        return client.files.download(path).contents.read()

    def put(path: str, content: bytes) -> None:
        client.files.upload(path, BytesIO(content), overwrite=False)

    def exists(path: str) -> bool:
        try:
            client.files.get_metadata(path)
            return True
        except NotFound:
            return False

    return FilesVolumeStore(read=read, put=put, exists=exists, publish=publish)
