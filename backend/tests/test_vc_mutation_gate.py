"""M04 ordered behavioral tests; all dependencies are VC/1.0 ports."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control import platform
from backend.services.version_control.coordination import CoordinationService
from backend.services.version_control.mutation_gate import MutationGate
from backend.tests.vc_fakes.fixtures import FakeCanonicalizer, binding_fixture, executor_fixture
from backend.tests.vc_fakes.stores import FakeCoordinationStore, FakeFactStore, ManualClock


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
    coordination.reject_reservation.side_effect = lambda *args: trace.append("reject_reservation")
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


def test_requested_only_history_proceeds_to_first_execution(rig):
    now = datetime.now(timezone.utc)
    inputs = vc.ApprovalInputs(
        "VC/1.0", rig.identity.operation_id, rig.request.operation_type,
        rig.request.source_version_id, rig.before.raw_state_digest, rig.before.fingerprints,
        None, None, rig.desired.state_digest, None,
        rig.before.fingerprints.canonicalizer_version, rig.binding, rig.before.fingerprints,
        None, "a" * 64, "b" * 64, "c" * 64, {}, "requester", {},
        now + timedelta(minutes=5),
    )
    approval_request = vc.ApprovalRequest(uid(), inputs, now)
    record = vc.ApprovalRecord(approval_request, (), vc.FactStatus.REQUESTED, None)
    requested = vc.OperationFact(
        uid(), vc.FactKind.APPROVAL_REQUEST, rig.identity.operation_id, 0,
        "approval-request", rig.binding, rig.identity, rig.request.operation_type,
        "requester", vc.ActorContext("requester", rig.binding.workspace_id, "human"),
        vc.FactStatus.REQUESTED, record, now, approval_id=approval_request.approval_id,
    )
    rig.facts.lookup_request.return_value = vc.RequestHistory((requested,), False)
    rig.facts.get_request.return_value = vc.ApprovedOperation(rig.request, None)

    result = rig.gate.execute(rig.request, rig.executor)

    assert result.status == vc.OperationStatus.CONFIRMED
    rig.coordination.reserve.assert_called_once()
    rig.coordination.admit.assert_called_once()
    rig.transport.patch_config_once.assert_called_once()
    rig.transport.patch_description_once.assert_called_once()


@pytest.mark.parametrize("status", [
    vc.FactStatus.APPLIED_UNVERIFIED,
    vc.FactStatus.APPLIED_PARTIAL,
    vc.FactStatus.CONFIRMED,
    vc.FactStatus.CONFLICTED,
])
def test_execution_outcome_history_routes_to_verify_only_without_second_patch(rig, status):
    fact = vc.OperationFact(
        uid(), vc.FactKind.OPERATION, rig.identity.operation_id, 0,
        "execution-outcome", rig.binding, rig.identity, rig.request.operation_type,
        "requester", vc.ActorContext("target-service", rig.binding.workspace_id, "service"),
        status, vc.StageEvidence("VC/1.0", vc.PatchStage.CONFIG_OBSERVED,
                                 datetime.now(timezone.utc), rig.identity.request_digest, None),
        datetime.now(timezone.utc),
    )
    rig.facts.lookup_request.return_value = vc.RequestHistory((fact,), False)
    rig.facts.get_request.return_value = vc.ApprovedOperation(rig.request, None)

    result = rig.gate.execute(rig.request, rig.executor)

    assert result.status == vc.OperationStatus.APPLIED_UNVERIFIED
    rig.coordination.reserve.assert_not_called()
    rig.transport.patch_config_once.assert_not_called()
    rig.transport.patch_description_once.assert_not_called()


def test_m08_service_executor_passes_m04_governed_write_identity_check(rig):
    client = Mock()
    client.config.host = "https://example.invalid"
    client.config.auth_type = "oauth-m2m"
    client.get_workspace_id.return_value = "123"
    client.current_user.me.return_value = SimpleNamespace(application_id="target-service", id="scim-id")
    client.api_client.do.return_value = {
        "userName": "target-service",
        "id": "scim-id",
        "X-Databricks-Org-Id": "123",
    }
    provider = platform.PlatformIdentityProvider(profiles={"target-test": lambda: client})
    executor = provider.executor(vc.ExplicitExecutorSelection(
        "123", "https://example.invalid", "target-service", "job/456", "target-test"))
    rig.gate._enabled(rig.binding, executor, "promotion")


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


@pytest.mark.parametrize('shared_facts', [False, True])
def test_pre_send_base_conflict_releases_binding_without_quarantine(rig, shared_facts):
    clock = ManualClock(datetime.now(timezone.utc))
    store = FakeCoordinationStore(clock)
    durable = FakeFactStore(clock)
    operation_durable = durable if shared_facts else FakeFactStore(clock)

    def facts_port(backing):
        def append(fact):
            backing.append(fact.event_key, vc.to_wire(fact))
            return vc.FactRef(fact.event_id, fact.event_key, 'd' * 64)

        def lookup(binding, key):
            facts = tuple(vc.from_wire(vc.OperationFact, row.payload) for row in backing.state.rows)
            return vc.RequestHistory(tuple(fact for fact in facts if fact.binding == binding
                and fact.request.idempotency_key == key), False)

        return SimpleNamespace(append=append, lookup_request=lookup)

    coordination_facts = facts_port(durable)
    rig.gate.facts = coordination_facts if shared_facts else facts_port(operation_durable)
    coordination = CoordinationService(
        store=store, facts=coordination_facts, ledger=rig.ledger, clock=clock,
        resolve_binding=lambda binding_id: rig.binding,
        verify_enrollment=lambda binding, proof: True, insert_enrolled=store.seed,
        validate_authorization=Mock(return_value=True), verify_create_intent=Mock(return_value=True),
        termination=Mock(spec=vc.TerminationEvidenceProvider),
        authorize_human_recovery=Mock(return_value=False), authorize_heads=Mock(return_value=False))
    coordination.initialize(rig.binding, vc.EnrollmentProof(uid(), rig.binding.binding_id, 1,
                                                           'serialized-enrollment'))
    rig.gate.coordination = coordination
    coordination.finish = Mock(wraps=coordination.finish)
    coordination.quarantine = Mock(wraps=coordination.quarantine)
    coordination.admit = Mock(wraps=coordination.admit)
    rig.state["description"] = "external edit"
    external = deepcopy(rig.state)
    result = rig.gate.execute(rig.request, rig.executor)
    assert result.operation_id == rig.identity.operation_id
    assert result.status == vc.OperationStatus.CONFLICTED
    assert not result.unresolved
    assert result.postimage is None
    rig.transport.patch_config_once.assert_not_called()
    rig.transport.patch_description_once.assert_not_called()
    assert rig.versions[result.preimage.version_id].snapshot.restorable_metadata["description"] == "external edit"
    assert rig.state == external
    coordination.quarantine.assert_not_called()
    coordination.finish.assert_not_called()
    coordination.admit.assert_not_called()
    rig.approvals.authorize.assert_not_called()
    row = store.read(rig.binding.binding_id)
    assert row.state == vc.CoordinationState.IDLE
    assert not row.unresolved
    assert len(durable.state.rows) == 1
    fact = vc.from_wire(vc.OperationFact, durable.state.rows[0].payload)
    assert fact.status == vc.FactStatus.CONFLICTED
    assert fact.request == rig.identity
    assert len(operation_durable.state.rows) == 1
    operation_fact = vc.from_wire(vc.OperationFact, operation_durable.state.rows[0].payload)
    assert operation_fact.status == vc.FactStatus.CONFLICTED
    assert operation_fact.request == rig.identity
    if not shared_facts:
        assert operation_fact.pre_version_id == result.preimage.version_id
    corrected = replace(rig.request, identity=vc.RequestIdentity(uid(), "corrected-key", "b" * 64),
                        expected_base=result.preimage.state_digest)
    reservation = coordination.reserve(corrected.binding, corrected.identity, rig.executor)
    assert reservation.request == corrected.identity
    assert store.read(rig.binding.binding_id).state == vc.CoordinationState.RESERVED


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


@pytest.mark.parametrize("drift", ["config", "benchmark", "metadata"])
def test_description_requires_expected_config_and_approved_metadata_preimage(rig, drift):
    def interfering_patch(*args):
        rig.config(*args)
        if drift == "metadata":
            rig.state["description"] = "external"
        else:
            rig.state["serialized_space"]["benchmarks" if drift == "benchmark" else "instructions"] = {"external": True}
    rig.transport.patch_config_once.side_effect = interfering_patch
    result = rig.gate.execute(rig.request, rig.executor)
    assert result.status == vc.OperationStatus.APPLIED_PARTIAL
    assert result.unresolved
    rig.transport.patch_description_once.assert_not_called()
    rig.coordination.quarantine.assert_called_once()


@pytest.mark.parametrize("status", [vc.FactStatus.APPLIED_PARTIAL, vc.FactStatus.CONFLICTED])
def test_partial_and_conflicted_publish_distinguishable_terminal_facts(rig, status):
    if status == vc.FactStatus.CONFLICTED:
        rig.state["description"] = "external edit"
    else:
        def interfering_patch(*args):
            rig.config(*args)
            rig.state["description"] = "external edit"
        rig.transport.patch_config_once.side_effect = interfering_patch
    result = rig.gate.execute(rig.request, rig.executor)
    terminal = [call.args[0] for call in rig.facts.append.call_args_list
                if call.args[0].status == status]
    assert len(terminal) == 1
    assert terminal[0].pre_version_id == result.preimage.version_id
    assert terminal[0].post_version_id == (result.postimage.version_id if result.postimage else None)
    boundary = "quarantine" if status == vc.FactStatus.APPLIED_PARTIAL else "reject_reservation"
    assert rig.trace.index("fact:" + status.value) < rig.trace.index(boundary)


def test_final_readback_must_match_all_fingerprints(rig):
    rig.transport.patch_description_once.side_effect = lambda *args: None
    result = rig.gate.execute(rig.request, rig.executor)
    assert result.status == vc.OperationStatus.APPLIED_PARTIAL
    assert result.unresolved


def test_config_timeout_matching_readback_never_replays_or_sends_description(rig):
    def lost_response(*args):
        rig.config(*args)
        raise TimeoutError("response lost after apply")
    rig.transport.patch_config_once.side_effect = lost_response
    result = rig.gate.execute(rig.request, rig.executor)
    assert result.status == vc.OperationStatus.APPLIED_UNVERIFIED
    assert result.unresolved
    rig.facts.get_request.return_value = vc.ApprovedOperation(rig.request, None)
    verified = rig.gate.verify_only(rig.identity.operation_id, rig.executor)
    assert verified.unresolved
    rig.transport.patch_config_once.assert_called_once()
    rig.transport.patch_description_once.assert_not_called()
    rig.coordination.finish.assert_not_called()


@pytest.mark.parametrize("error", [TimeoutError("timeout"), RuntimeError("HTTP 503")])
def test_description_timeout_and_5xx_quarantine_without_replay(rig, error):
    def lost_response(*args):
        rig.description(*args)
        raise error
    rig.transport.patch_description_once.side_effect = lost_response
    result = rig.gate.execute(rig.request, rig.executor)
    assert result.status == vc.OperationStatus.APPLIED_UNVERIFIED
    assert result.unresolved
    rig.transport.patch_config_once.assert_called_once()
    rig.transport.patch_description_once.assert_called_once()
    rig.coordination.quarantine.assert_called_once()
    rig.coordination.finish.assert_not_called()


@pytest.mark.parametrize("read_number", [2, 3])
def test_successful_patch_then_failed_readback_is_applied_unverified(rig, read_number):
    reads = 0
    def get(*args):
        nonlocal reads
        reads += 1
        if reads == read_number:
            raise RuntimeError("GET unavailable")
        return rig.get(*args)
    rig.transport.get.side_effect = get
    result = rig.gate.execute(rig.request, rig.executor)
    assert result.status == vc.OperationStatus.APPLIED_UNVERIFIED
    assert result.unresolved
    rig.coordination.finish.assert_not_called()
    rig.coordination.quarantine.assert_called_once()
    assert rig.facts.append.call_args.args[0].status == vc.FactStatus.APPLIED_UNVERIFIED


@pytest.mark.parametrize("boundary", ["config_before", "config_after", "description_before", "description_after"])
def test_worker_crash_at_every_send_boundary_keeps_attempt_unresolved(rig, boundary):
    transport_method = rig.transport.patch_config_once if boundary.startswith("config") else rig.transport.patch_description_once
    original = rig.config if boundary.startswith("config") else rig.description
    stage = "config_in_flight" if boundary.startswith("config") else "description_in_flight"
    def crash(*args):
        assert stage in rig.trace, "Durable send intent must precede even a crashing send"
        if boundary.endswith("after"):
            original(*args)
        raise SystemExit("worker died")
    transport_method.side_effect = crash
    with pytest.raises(SystemExit):
        rig.gate.execute(rig.request, rig.executor)
    rig.coordination.finish.assert_not_called()
    assert stage in rig.trace
    rig.facts.get_request.return_value = vc.ApprovedOperation(rig.request, None)
    assert rig.gate.verify_only(rig.identity.operation_id, rig.executor).unresolved
    assert transport_method.call_count == 1


def test_failed_final_fact_publish_never_releases_attempt(rig):
    def append(fact):
        if fact.status == vc.FactStatus.CONFIRMED:
            raise RuntimeError("lost fact response")
        return vc.FactRef(fact.event_id, fact.event_key, "a" * 64)
    rig.facts.append.side_effect = append
    result = rig.gate.execute(rig.request, rig.executor)
    assert result.unresolved
    rig.coordination.finish.assert_not_called()
    rig.coordination.quarantine.assert_called_once()


def test_resumed_request_never_enters_a_new_send_sequence(rig):
    rig.gate.execute(rig.request, rig.executor)
    rig.state.update(vc.to_wire(rig.before.response_envelope))
    rig.facts.lookup_request.return_value = vc.RequestHistory(
        tuple(call.args[0] for call in rig.facts.append.call_args_list), False)
    rig.facts.get_request.return_value = vc.ApprovedOperation(rig.request, None)
    rig.gate.execute(rig.request, rig.executor)
    rig.transport.patch_config_once.assert_called_once()
    rig.transport.patch_description_once.assert_called_once()


@pytest.mark.parametrize("flags", [None, False])
def test_all_existing_mutation_entrypoints_use_gate_and_fail_closed(rig, flags):
    if flags is None:
        rig.gate.flags = None
    else:
        rig.flags.enabled.return_value = False
    with pytest.raises(PermissionError, match="disabled"):
        rig.gate.execute(rig.request, rig.executor)
    rig.coordination.reserve.assert_not_called()
    rig.transport.patch_config_once.assert_not_called()


@pytest.mark.parametrize("operation_type", ["restore", "promotion", "optimizer_apply", "reapply"])
def test_governed_operations_cannot_use_app_executor(rig, operation_type):
    with pytest.raises(PermissionError):
        rig.gate.execute(replace(rig.request, operation_type=operation_type),
                         replace(rig.executor, execution_ref="app/worker"))
    rig.transport.patch_config_once.assert_not_called()
