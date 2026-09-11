"""Simple in-workspace restore (CUJ-1 §4.5, Option 1).

Restore = write a stored historical ``serialized_space`` back onto the live Genie space
**as the logged-in user (OBO)**, guarded by an optimistic concurrency check, then record
the result as a new ``origin=restore`` version. This is the same trust model the rest of
the workbench uses to write spaces (the user's own edit rights are the enforcement) — it
does NOT use the governed mutation gate / approvals / Job (that heavyweight path stays
dormant behind ``FailClosedRestore`` for regulated/cross-workspace needs).

The two I/O seams are injected so the whole orchestration is offline-testable:

* ``live_reader(space_id) -> envelope`` — GET the live serialized space (OBO).
* ``live_writer(space_id, serialized_space, description)`` — PATCH it back (OBO; NO SP
  fallback, so a caller without edit rights is correctly rejected by the API).

Safety (the one real concern of a naive overwrite): before writing, we canonicalize the
live state and require it to still equal the version the user believed was current
(``expected_current_version_id``). If the space moved since they looked, we refuse with a
409 rather than clobbering the concurrent change.
"""

from backend.services.version_control import contracts as vc


def restore_space_version(runtime, *, space_id, version_id, expected_current_version_id,
                          actor, live_reader, live_writer) -> vc.ObservationResult:
    """Restore ``version_id`` onto ``space_id`` under ``actor`` (OBO). Returns the recorded
    ``ObservationResult`` (its ``captured_version`` is the new ``restore`` version, or the
    unchanged head when the live space already equals the requested version).

    Raises (mapped to HTTP by the router's ``_invoke``):
      * ``LookupError`` — space not enrolled / requested version unknown (404).
      * ``PermissionError`` — actor is not scoped to the binding (403).
      * ``ValueError`` — the client's view is stale or the space drifted (409).
    """
    binding = runtime.registry.find_active_by_space_key(space_id)
    if binding is None:
        raise LookupError("Space is not enrolled in version control")
    if (actor.workspace_id != binding.workspace_id
            or runtime.authorize_history(actor, binding) is not True):
        raise PermissionError("Binding history scope denied")

    historical = runtime.ledger.get_version(binding, version_id)  # KeyError -> 404
    try:
        expected = runtime.ledger.get_version(binding, expected_current_version_id)
    except KeyError as error:
        raise ValueError("Current version is stale; refresh the history and retry") from error

    live = runtime.canonicalizer.observe(live_reader(space_id))
    if runtime.canonicalizer.compare(live, expected.snapshot) is not vc.Comparison.EQUAL:
        raise ValueError("The space changed since you last viewed it; refresh and retry")

    live_writer(space_id, historical.snapshot.serialized_space,
                historical.snapshot.restorable_metadata.get("description"))

    executor = runtime.identity.executor(runtime.reader_selection)
    return runtime.observer.capture(binding, "restore", executor, origin=vc.Origin.RESTORE,
                                    restored_from_version_id=version_id)
