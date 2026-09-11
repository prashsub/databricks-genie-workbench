"""Real-platform storage capability probe (SP-only first-write gate).

DEPLOYMENT BLOCKER, not a passing test: the `live_platform` fixture
(backend/tests/integration/conftest.py) FAILS with an explicit blocker reason
when VC_INTEGRATION_CONFIG is unset, so an unconfigured environment can never be
scored as green. Deselected by default; run only under the sanctioned gate:

    python -m pytest backend/tests/integration -m integration -q

This gate proves the SP-only STORAGE_CAPABILITIES subset — fine-grained DML,
non-owner runtime, append-only, Serializable coordination, isolated enrollment —
is actually satisfied by the provisioned namespace (the six runtime SPs, no human
identity or cross-workspace topology needed), and that `storage_write_ready`
degrades closed the instant any capability is missing or the probe cannot reach
the platform. The full FIRST_WRITE_CAPABILITIES gate additionally requires the
audit, identity and topology evidence proved by the other integration gates.
"""

import pytest

from backend.services.version_control import platform
from backend.services.version_control.platform.capabilities import (
    STORAGE_CAPABILITIES,
)

pytestmark = pytest.mark.integration


def test_storage_write_capabilities_are_live_and_degrade_closed(live_platform):
    evidence = live_platform.storage_capability_probe()
    # The live probe covers exactly the SP-only storage subset, all satisfied.
    assert set(evidence) == set(STORAGE_CAPABILITIES)
    assert all(evidence[capability] is True for capability in STORAGE_CAPABILITIES), evidence
    assert platform.storage_write_ready(live_platform.storage_capability_probe)

    # A missing capability degrades closed (no earlier positive is reused).
    for missing in STORAGE_CAPABILITIES:
        degraded = {name: value for name, value in evidence.items() if name != missing}
        assert not platform.storage_write_ready(lambda degraded=degraded: degraded)

    # A probe that cannot reach the platform is never a positive result.
    def outage():
        raise TimeoutError("coordination/evidence outage")

    assert not platform.storage_write_ready(outage)
    assert not platform.storage_write_ready(None)
