"""M04 ordered behavioral tests; all dependencies are VC/1.0 ports."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.mutation_gate import MutationGate
from backend.tests.vc_fakes.fixtures import FakeCanonicalizer, binding_fixture, executor_fixture


def uid():
    return str(uuid4())


@pytest.fixture
def rig():
    trace = []
    canonicalizer = FakeCanonicalizer()
    binding = binding_fixture()
    executor = executor_fixture()
    state = {"serialized_space": {"instructions": "old"}, "description": "old"}
    before = canonicalizer.observe(state)
    identity = vc.RequestIdentity(uid(), "edit-key", "a" * 64)
    request = vc.MutationRequest(identity, binding, "edit", uid(), before.state_digest,
                                 {"instructions": "new"}, "new", None)
    fence = vc.FenceToken(binding.binding_id, 1, uid(), 1, 1)
    coordination = Mock(spec=vc.Coordination)
    ledger = Mock(spec=vc.VersionLedger)
    facts = Mock(spec=vc.OperationFacts)
    approvals = Mock(spec=vc.ApprovalService)
    transport = Mock(spec=vc.GenieTransport)
    flags = Mock()
    flags.enabled.return_value = True
    versions = {}

    def append(snapshot, context):
        trace.append("commit:" + context.observation_reason)
        version = vc.Version(uid(), snapshot, context)
        versions[version.version_id] = version
        return vc.ObservationRef(version.version_id, binding.binding_id, 1,
                                 snapshot.state_digest, snapshot.response_envelope_digest)

    def admit(reservation, preimage, authorization):
        trace.append("admit")
        return vc.AdmissionClaim(**vc.to_wire(fence), request=reservation.request,
                                 preimage=preimage, approval_id=None, approval_digest=None)

    def get(*args):
        trace.append("get")
        return deepcopy(state)

    def config(bound, serialized, claim):
        trace.append("patch_config")
        state["serialized_space"] = deepcopy(serialized)

    def description(bound, text, claim):
        trace.append("patch_description")
        state["description"] = text

    coordination.reserve.side_effect = lambda *args: (trace.append("reserve") or vc.Reservation(
        fence, args[1], executor, datetime.now(timezone.utc) + timedelta(minutes=5)))
    coordination.admit.side_effect = admit
    coordination.assert_owner.side_effect = lambda *args: trace.append("assert_owner")
    coordination.checkpoint.side_effect = lambda claim, stage, evidence: trace.append(stage.value)
    coordination.finish.side_effect = lambda *args: trace.append("finish")
    coordination.quarantine.side_effect = lambda *args: trace.append("quarantine")
    ledger.append_observation.side_effect = append
    ledger.verify_committed.side_effect = lambda ref: trace.append("verify_commit") or True
    ledger.get_version.side_effect = lambda bound, version_id: versions[version_id]
    facts.lookup_request.return_value = vc.RequestHistory((), False)
    facts.verify_flush.return_value = True
    facts.append.side_effect = lambda fact: trace.append("fact:" + fact.status.value) or vc.FactRef(
        fact.event_id, fact.event_key, "a" * 64)
    desired = canonicalizer.observe({"serialized_space": dict(request.serialized_space), "description": request.description})
    approvals.authorize.side_effect = lambda req, exe: trace.append("authorize") or vc.AuthorizationGrant(
        req.binding, req.identity, "grant", before.fingerprints, desired.state_digest,
        datetime.now(timezone.utc) + timedelta(minutes=5), None, None)
    transport.get.side_effect = get
    transport.patch_config_once.side_effect = config
    transport.patch_description_once.side_effect = description
    gate = MutationGate(coordination=coordination, ledger=ledger, facts=facts,
                        approvals=approvals, transport=transport, canonicalizer=canonicalizer, flags=flags)
    return SimpleNamespace(**locals())


def test_gate_has_no_transport_write_before_reserve_evidence_and_admit(rig):
    rig.gate.execute(rig.request, rig.executor)
    assert rig.trace[:6] == ["reserve", "get", "commit:preimage", "verify_commit", "authorize", "admit"]
    assert rig.trace.index("admit") < rig.trace.index("patch_config")


@pytest.mark.parametrize("failure", ["reserve", "append_observation", "verify_committed", "authorize", "admit"])
def test_required_port_failure_never_writes(rig, failure):
    owner = {"reserve": rig.coordination, "append_observation": rig.ledger,
             "verify_committed": rig.ledger, "authorize": rig.approvals, "admit": rig.coordination}[failure]
    getattr(owner, failure).side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        rig.gate.execute(rig.request, rig.executor)
    rig.transport.patch_config_once.assert_not_called()
    rig.transport.patch_description_once.assert_not_called()


def test_noop_all_three_fingerprints_skips_both_patches(rig):
    request = replace(rig.request, serialized_space=rig.state["serialized_space"], description="old")
    result = rig.gate.execute(request, rig.executor)
    assert result.status == vc.OperationStatus.NOOP
    rig.transport.patch_config_once.assert_not_called()
    rig.transport.patch_description_once.assert_not_called()
    assert rig.facts.append.call_args.args[0].status == vc.FactStatus.NOOP
    rig.coordination.finish.assert_called_once()


@pytest.mark.parametrize("field", ["instructions", "benchmarks"])
def test_noop_requires_config_and_benchmark_equality(rig, field):
    request = replace(rig.request, serialized_space={**rig.state["serialized_space"], field: {"value": "changed"}}, description="old")
    rig.gate.execute(request, rig.executor)
    rig.transport.patch_config_once.assert_called_once()


def test_reviewed_base_mismatch_preserves_external_preimage_and_blocks(rig):
    rig.state["description"] = "external edit"
    result = rig.gate.execute(rig.request, rig.executor)
    assert result.status == vc.OperationStatus.CONFLICTED
    assert rig.versions[result.preimage.version_id].snapshot.restorable_metadata["description"] == "external edit"
    rig.coordination.admit.assert_not_called()
    rig.transport.patch_config_once.assert_not_called()
    rig.transport.patch_description_once.assert_not_called()


def test_two_patch_happy_path_checkpoints_and_reasserts_each_send(rig):
    result = rig.gate.execute(rig.request, rig.executor)
    assert result is not None, "Both PATCHes need checkpointed final evidence"
    assert result.status == vc.OperationStatus.CONFIRMED
    assert rig.trace.count("admit") == 1
    for send in ("patch_config", "patch_description"):
        assert rig.trace[rig.trace.index(send) - 1] == "assert_owner"
    assert rig.trace.index("config_observed") < rig.trace.index("patch_description")
    assert rig.trace.index("description_observed") < rig.trace.index("finish")
    version = rig.versions[result.postimage.version_id]
    assert version.context.parent_version_id == result.preimage.version_id
    assert version.context.attempt_id == rig.fence.attempt_id
    assert version.context.generation == rig.fence.generation
    assert version.snapshot.fingerprints == rig.desired.fingerprints
