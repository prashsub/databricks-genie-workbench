"""Live capability checks; outages never reuse an earlier positive result."""

STORAGE_CAPABILITIES = frozenset({
    "fine_grained_dml", "serializable", "nonowner_runtime", "append_only", "enrollment_isolated",
})
FIRST_WRITE_CAPABILITIES = STORAGE_CAPABILITIES | frozenset({
    "coordination_available", "evidence_available", "authorization_available", "identity_verified",
    "owner_handlers_integrated", "canonicalization_verified", "recovery_policy_verified",
    "live_permissions_verified", "live_concurrency_verified", "failure_matrix_verified",
    "bundle_authority_verified", "routed_write_coverage", "artifact_permissions_verified",
})
REQUIRED_WRITER_PATHS = frozenset({
    "create", "edit", "restore", "reconcile", "optimizer_apply", "promotion", "compensation",
})


def capabilities_ready(probe, required) -> bool:
    if probe is None:
        return False
    try:
        evidence = probe()
        return all(evidence.get(capability) is True for capability in required)
    except Exception:
        return False


def storage_write_ready(probe) -> bool:
    return capabilities_ready(probe, STORAGE_CAPABILITIES)
