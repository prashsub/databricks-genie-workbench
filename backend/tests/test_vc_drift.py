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


@pytest.mark.parametrize("case", ["missing_head", "missing_history", "unsupported", "mixed"])
def test_missing_history_or_incompatible_canonicalizer_is_unknown(case):
    from backend.services.version_control.drift import DriftService

    original = snapshot("one")
    unsupported = replace(original, fingerprints=replace(original.fingerprints, canonicalizer_version="future/9"))
    states = {version_id(1): original, version_id(2): original}
    heads = vc.Heads(version_id(1), version_id(2), version_id(1))
    if case == "missing_head":
        heads = replace(heads, approved=None)
    elif case == "missing_history":
        del states[version_id(2)]
    elif case == "unsupported":
        states = dict.fromkeys(states, unsupported)
    else:
        states[version_id(2)] = unsupported
    result = DriftService(canonicalizer=FakeCanonicalizer()).classify(heads, lookup(states), "reachable")
    assert result.state == vc.DriftState.UNKNOWN
    assert result.allowed_actions == ()
    assert result.reasons


@pytest.mark.parametrize("change", ["metadata", "array_order"])
def test_meaningful_changes_are_not_clean(change):
    from backend.services.version_control.drift import DriftService

    canonicalizer = FakeCanonicalizer()
    before = canonicalizer.observe({"serialized_space": {"queries": ["first", "second"]}, "description": "old"})
    after = canonicalizer.observe({"serialized_space": {"queries": ["second", "first"] if change == "array_order"
                                                    else ["first", "second"]}, "description": "new"})
    result = DriftService(canonicalizer=canonicalizer).classify(
        vc.Heads(version_id(2), version_id(1), version_id(1)),
        lookup({version_id(1): before, version_id(2): after}), "reachable")
    assert result.state == vc.DriftState.EXTERNAL_AHEAD
