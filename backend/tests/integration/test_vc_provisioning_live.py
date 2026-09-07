"""Real-platform DAB validate + repeatable sandbox provisioning probe (plan task 16).

Kept distinct from the permission probes so the DAB-CLI probe is separable. This is a
DEPLOYMENT BLOCKER, not a passing test: the `live_platform` fixture FAILS with an
explicit blocker reason when VC_INTEGRATION_CONFIG is unset. Deselected by default;
runs only under:

    python -m pytest backend/tests/integration -m integration -q

Plan task 16 nominally colocated this in test_vc_provisioning.py (integration marker);
per m08 fixspec B5 it lives here under backend/tests/integration/ so a single sanctioned
command remains authoritative for the whole integration layer.
"""

from pathlib import Path

import pytest

from backend.services.version_control import platform

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_dab_validate_and_sandbox_provision_are_repeatable(live_platform):
    platform.repeatable_sandbox_provision(ROOT, live_platform.config["bundle"], live_platform)
