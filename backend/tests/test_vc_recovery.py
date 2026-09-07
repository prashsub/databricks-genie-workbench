"""Recovery never infers executor termination from a matching Genie GET."""
from dataclasses import replace
from datetime import timedelta

import pytest

from backend.services.version_control import contracts as c
from backend.services.version_control.coordination import CoordinationError
from backend.tests.test_vc_coordination import h, enroll, reserve, admit, durable_facts, uid


@pytest.mark.parametrize('entry', ['reserve', 'renew', 'assert_owner'])
def test_expired_lease_quarantines_without_takeover(h, entry):
    enroll(h)
    claim = admit(h)
    before = h.store.read(h.binding.binding_id)
    h.clock.advance(timedelta(seconds=61))
    with pytest.raises(CoordinationError):
        if entry == 'reserve':
            reserve(h)
        else:
            getattr(h.service, entry)(claim)
    row = h.store.read(h.binding.binding_id)
    assert row.state == c.CoordinationState.QUARANTINED
    assert row.unresolved
    assert (row.attempt_id, row.generation, row.approval_id, row.pre_version_id) == (
        before.attempt_id, before.generation, before.approval_id, before.pre_version_id)
    assert any(call.args[0].status == c.FactStatus.QUARANTINED for call in h.facts.append.call_args_list)
    with pytest.raises(CoordinationError):
        reserve(h)
