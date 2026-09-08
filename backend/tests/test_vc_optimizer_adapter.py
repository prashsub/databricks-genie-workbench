"""M10 VC/1.0 seam tests: backend services are Protocol fakes only."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from backend.services.version_control import contracts as vc
from backend.services.version_control.optimizer_adapter import (
    ChampionArtifact, ChampionMutationRequest, OptimizerChampionAdapter, source_digest,
)
from backend.services.version_control.governance.approvals import request_digest
from backend.services.version_control.governance.facts import DurableOperationFacts, fact_key
from backend.services.version_control.platform.feature_flags import FeatureFlags
from backend.tests.vc_fakes.fixtures import FakeIdentityProvider, binding_fixture, executor_fixture
from backend.tests.vc_fakes.stores import FakeFactStore, ManualClock


@pytest.fixture
def rig():
    binding = binding_fixture()
    executor = executor_fixture()
    payload = {"version": 2, "instructions": {}}
    fingerprints = vc.Fingerprints("a" * 64, "b" * 64, "c" * 64, "vc-c14n/1")
    source = ChampionArtifact("run", "champion", binding, fingerprints.state_digest,
                              str(uuid4()), payload, "description",
                              source_digest(payload, "description"), "requester", str(uuid4()))
    sources = Mock()
    sources.load.return_value = source
    gate = Mock(spec=vc.MutationGate)
    gate.execute.return_value = vc.OperationResult(str(uuid4()), vc.OperationStatus.CONFIRMED,
                                                   None, None, False, ())
    requests = Mock()
    requests.prepare.side_effect = lambda request, requester: request
    facts = Mock(spec=vc.OperationFacts)
    facts.get_request.side_effect = lambda operation_id: vc.ApprovedOperation(
        requests.prepare.call_args.args[0], None)
    flags = Mock()
    flags.enabled.return_value = True
    identity = Mock(spec=vc.IdentityProvider)
    identity.can_edit.return_value = True
    approvals = Mock(spec=vc.ApprovalService)
    approvals.authorize.side_effect = lambda request, job: vc.AuthorizationGrant(
        request.binding, request.identity, "requester-rights", fingerprints,
        vc.canonical_json_hash("vc-rendered-target/1", {
            "serialized_space": request.serialized_space, "description": request.description,
        }), datetime.now(timezone.utc) + timedelta(minutes=5), request.approval_id, None)
    adapter = OptimizerChampionAdapter(sources=sources, gate=gate, requests=requests, flags=flags,
                                       facts=facts, identity=identity, approvals=approvals)
    return adapter, source, executor, gate, requests, flags


def test_final_champion_applies_once_through_shared_gate(rig):
    adapter, source, executor, gate, requests, _ = rig
    receipt = adapter.apply(source.run_id, source.champion_id, source.binding,
                            source.expected_base, executor)
    assert receipt is gate.execute.return_value
    request, job = gate.execute.call_args.args
    assert job is executor
    assert request.operation_type == "optimizer_apply"
    assert request.origin == vc.Origin.OPTIMIZER
    assert (request.optimizer_run_id, request.champion_id) == ("run", "champion")
    assert request.serialized_space == source.serialized_space
    requests.prepare.assert_called_once_with(request, "requester")
    gate.execute.assert_called_once()


def test_champion_request_digest_satisfies_m06_binding(rig):
    adapter, source, executor, gate, _, _ = rig
    adapter.apply(source.run_id, source.champion_id, source.binding,
                  source.expected_base, executor)
    request = gate.execute.call_args.args[0]
    assert request.identity.request_digest == request_digest(request)
    assert request.source_payload_digest == source_digest(
        source.serialized_space, source.description)
    assert request.identity.request_digest != request.source_payload_digest


def test_champion_apply_authorizes_through_real_approval_service(rig):
    from backend.services.version_control.governance.approvals import ApprovalService

    def build_adapter(*, approval_id=True, authorization_fault=None):
        adapter, source, executor, gate, requests, _ = rig
        gate.reset_mock()
        clock = ManualClock(datetime.now(timezone.utc))
        store = FakeFactStore(clock)
        durable_facts = DurableOperationFacts(
            lambda: tuple(row.payload for row in store.state.rows), store.append,
            writes_enabled=True)

        class ChampionFacts:
            def __init__(self):
                self.requests = {}

            def __getattr__(self, name):
                return getattr(durable_facts, name)

            def record_request(self, request, fact):
                self.requests[request.identity.operation_id] = request
                durable_request = vc.MutationRequest(
                    request.identity, request.binding, request.operation_type,
                    request.source_version_id, request.expected_base,
                    request.serialized_space, request.description, request.approval_id)
                return durable_facts.record_request(durable_request, fact)

            def get_request(self, operation_id):
                approved = durable_facts.get_request(operation_id)
                request = self.requests.get(operation_id, approved.request)
                return replace(approved, request=request)

        facts = ChampionFacts()
        identity = FakeIdentityProvider()
        target_identity = FakeIdentityProvider()
        identity.run_as[executor.execution_ref] = executor.principal_id
        target_identity.run_as[executor.execution_ref] = executor.principal_id
        requester = vc.ActorContext("requester", source.binding.workspace_id, "human")
        identity.actors[requester.subject_id] = requester
        identity.edit_rights.add((requester.subject_id, source.binding.workspace_id,
                                  source.binding.binding_id, source.binding.binding_revision))
        for approver in ("human-1", "human-2"):
            identity.actors[approver] = vc.ActorContext(
                approver, source.binding.workspace_id, "human")
            identity.memberships[(approver, source.binding.workspace_id)] = frozenset({"approvers"})
            target_identity.memberships[(approver, source.binding.workspace_id)] = frozenset(
                {"target-approvers"})
        registry = Mock()
        registry.resolve.return_value = source.binding
        approvals = ApprovalService(
            facts, identity, registry, clock.now,
            target_identity=lambda selected: target_identity, writes_enabled=True)

        key = vc.canonical_json_hash("vc-optimizer-run/1", {
            "run_id": source.run_id, "binding_id": source.binding.binding_id,
            "binding_revision": source.binding.binding_revision,
            "workspace_id": source.binding.workspace_id,
        })
        operation_id = str(uuid5(NAMESPACE_URL, "vc-optimizer-run/1:" + key))
        source = replace(source, approval_id=operation_id if approval_id else None)
        adapter.sources.load.return_value = source
        adapter.facts = facts
        adapter.identity = identity
        adapter.approvals = approvals

        def prepare(request, requester_id):
            preflight_digest = "d" * 64
            evidence = vc.StageEvidence(
                "VC/1.0", vc.PatchStage.CONFIG_PENDING, clock.now(), preflight_digest, None)
            provisional = vc.OperationFact(
                operation_id, vc.FactKind.OPERATION, operation_id, 0, "", request.binding,
                request.identity, request.operation_type, requester_id, requester,
                vc.FactStatus.REQUESTED, evidence, clock.now())
            event_key = fact_key(provisional)
            fact = replace(provisional, event_key=event_key,
                           event_id=str(uuid5(NAMESPACE_URL, event_key)))
            facts.record_request(request, fact)
            if request.approval_id is None:
                return request
            fingerprints = vc.Fingerprints(
                "a" * 64, "b" * 64, "c" * 64, "vc-c14n/1")
            inputs = vc.ApprovalInputs(
                "VC/1.0", request.identity.operation_id, request.operation_type,
                request.source_version_id, request.source_payload_digest, fingerprints,
                None, None,
                vc.canonical_json_hash("vc-rendered-target/1", {
                    "serialized_space": request.serialized_space,
                    "description": request.description,
                }),
                None, "vc-c14n/1", request.binding, fingerprints, None,
                "e" * 64, "f" * 64, preflight_digest, {"minimum": 2}, requester_id,
                {"verify_only": True}, clock.now() + timedelta(hours=1))
            approval = approvals.request(inputs, requester)
            for approver in ("human-1", "human-2"):
                approvals.vote(approval.approval_id, "approve", identity.actors[approver])
            if authorization_fault == "expired":
                clock.advance(timedelta(hours=2))
            elif authorization_fault == "revoked-group":
                identity.memberships.clear()
            return request

        requests.prepare.side_effect = prepare
        return adapter, source, executor, gate

    adapter, source, executor, gate = build_adapter()
    assert adapter.apply(source.run_id, source.champion_id, source.binding,
                         source.expected_base, executor) is gate.execute.return_value
    assert isinstance(adapter.approvals.authorize(
        gate.execute.call_args.args[0], executor), vc.AuthorizationGrant)
    gate.execute.assert_called_once()

    adapter, source, executor, gate = build_adapter(approval_id=False)
    with pytest.raises(PermissionError):
        adapter.apply(source.run_id, source.champion_id, source.binding,
                      source.expected_base, executor)
    gate.execute.assert_not_called()

    for fault in ("expired", "revoked-group"):
        adapter, source, executor, gate = build_adapter(authorization_fault=fault)
        with pytest.raises(PermissionError):
            adapter.apply(source.run_id, source.champion_id, source.binding,
                          source.expected_base, executor)
        gate.execute.assert_not_called()


def test_champion_apply_rejects_app_executor_or_tampered_source(rig):
    adapter, source, executor, gate, _, _ = rig
    with pytest.raises(PermissionError):
        adapter.apply("run", "champion", source.binding, source.expected_base,
                      replace(executor, execution_ref="app/worker"))
    adapter.sources.load.return_value = replace(source, payload_digest="b" * 64)
    with pytest.raises(ValueError, match="digest"):
        adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    gate.execute.assert_not_called()


def test_optimizer_write_switches_default_off(rig):
    adapter, source, executor, gate, _, flags = rig
    adapter.flags = None
    with pytest.raises(PermissionError):
        adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    gate.execute.assert_not_called()


def test_adapter_uses_only_m08_declared_switches(rig):
    adapter, source, executor, gate, _, _ = rig
    disabled = FeatureFlags()
    adapter.flags = Mock(wraps=disabled)
    with pytest.raises(PermissionError, match="disabled"):
        adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    gate.execute.assert_not_called()

    enabled = FeatureFlags(vc_writes_enabled=True, vc_optimizer_apply_enabled=True)
    adapter.flags = Mock(wraps=enabled)
    assert adapter.apply("run", "champion", source.binding, source.expected_base,
                         executor) is gate.execute.return_value
    checked_names = [call.args[0] for call in adapter.flags.enabled.call_args_list]
    assert checked_names
    assert set(checked_names) <= set(enabled.registry)
    gate.execute.assert_called_once()


def test_retry_or_second_champion_in_same_run_cannot_append_second_optimizer_version(rig):
    adapter, source, executor, gate, requests, _ = rig
    durable_requests = {}
    versions = {}

    def prepare(request, requester):
        existing = durable_requests.setdefault(request.identity.idempotency_key, request)
        if existing.identity.request_digest != request.identity.request_digest:
            raise ValueError("Run already has a different champion/digest")
        return existing

    def execute(request, job):
        return versions.setdefault(request.identity.operation_id, gate.execute.return_value)

    requests.prepare.side_effect = prepare
    gate.execute.side_effect = execute
    first = adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    restarted = OptimizerChampionAdapter(sources=adapter.sources, gate=gate,
                                         requests=requests, flags=adapter.flags, facts=adapter.facts,
                                         identity=adapter.identity, approvals=adapter.approvals)
    assert restarted.apply("run", "champion", source.binding, source.expected_base,
                            replace(executor, execution_ref="job/new-attempt")) is first
    for changed in (replace(source, champion_id="other"),
                    replace(source, description="changed",
                            payload_digest=source_digest(source.serialized_space, "changed"))):
        adapter.sources.load.return_value = changed
        with pytest.raises(ValueError, match="different champion/digest"):
            adapter.apply("run", changed.champion_id, source.binding, source.expected_base, executor)
    assert len(durable_requests) == len(versions) == 1
    assert gate.execute.call_count == 2


def test_telemetry_memory_fallback_never_authorizes_champion_apply(rig):
    adapter, source, executor, gate, requests, _ = rig
    adapter.facts = Mock(spec=vc.OperationFacts)
    adapter.facts.get_request.side_effect = ConnectionError("authoritative store unavailable")
    adapter.sources.telemetry_fallback = {"champion": source}
    with pytest.raises(ConnectionError, match="authoritative store"):
        adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    gate.execute.assert_not_called()
    requests.prepare.assert_called_once()


def test_job_executor_verifies_requester_edit_or_release_policy(rig):
    adapter, source, executor, gate, _, _ = rig
    source = replace(source, approval_id=None)
    adapter.sources.load.return_value = source
    adapter.identity = Mock(spec=vc.IdentityProvider)
    adapter.identity.can_edit.side_effect = lambda subject, binding: subject == executor.principal_id
    adapter.approvals = Mock(spec=vc.ApprovalService)
    with pytest.raises(PermissionError, match="approval"):
        adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    adapter.identity.can_edit.assert_not_called()
    gate.execute.assert_not_called()


def test_release_policy_denial_and_wrong_job_run_as_fail_closed(rig):
    adapter, source, executor, gate, _, _ = rig
    source = replace(source, approval_id=str(uuid4()), binding=replace(source.binding, environment="prod"))
    adapter.sources.load.return_value = source
    adapter.identity = Mock(spec=vc.IdentityProvider)
    adapter.identity.can_edit.return_value = False
    adapter.approvals = Mock(spec=vc.ApprovalService)
    adapter.approvals.authorize.side_effect = PermissionError("release policy revoked")
    with pytest.raises(PermissionError, match="release policy"):
        adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    adapter.identity.verify_run_as.side_effect = PermissionError("wrong run_as")
    with pytest.raises(PermissionError, match="run_as"):
        adapter.apply("run", "champion", source.binding, source.expected_base, executor)
    gate.execute.assert_not_called()
