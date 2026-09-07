"""Live capability checks; outages never reuse an earlier positive result."""

STORAGE_CAPABILITIES = frozenset({
    "fine_grained_dml", "serializable", "nonowner_runtime", "append_only", "enrollment_isolated",
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
