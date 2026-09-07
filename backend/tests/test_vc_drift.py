from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID

import pytest

from backend.services.version_control import contracts as vc
from backend.tests.vc_fakes.fixtures import FakeCanonicalizer


def version_id(number):
    return str(UUID(int=number))


def snapshot(description):
    return FakeCanonicalizer().observe({"serialized_space": {}, "description": description})


def lookup(states):
    port = Mock(spec=vc.VersionLookup)
    port.get.side_effect = states.__getitem__
    return port


@pytest.mark.parametrize("observed,approved,deployed,expected", [
    (1, 1, 1, "clean"), (2, 1, 1, "external_ahead"),
    (1, 2, 1, "desired_ahead"), (2, 3, 1, "diverged"),
    (2, 2, 1, "desired_ahead"),
])
def test_three_heads_classify_clean_external_desired_diverged(observed, approved, deployed, expected):
    from backend.services.version_control.drift import DriftService

    versions = lookup({version_id(number): snapshot(str(number)) for number in (1, 2, 3)})
    result = DriftService(canonicalizer=FakeCanonicalizer()).classify(
        vc.Heads(*(version_id(number) for number in (observed, approved, deployed))), versions, "reachable")
    assert result.state.value == expected
    assert result.policy_target == version_id(approved)
    assert result.common_base == (version_id(1) if observed == 1 or approved == 1 else None)
    assert result.reasons
