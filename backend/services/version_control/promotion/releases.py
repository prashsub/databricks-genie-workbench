"""Target-local release admission over immutable operation facts."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control import contracts as vc
from backend.services.version_control.governance.approvals import request_digest
from backend.services.version_control.governance.facts import fact_key
from . import receipts


ADMISSION_REFERENCE = 'target-local-promotion-approved'


@dataclass(frozen=True)
class PinnedRelease:
    release_id: str
    package: vc.PackageRef
    target_binding: vc.BindingRef
    target_host: str


class ReleaseCatalog:
    def __init__(self, read_release_facts, outbound_volume, target_host):
        self.read_release_facts = read_release_facts
        self.outbound_volume = outbound_volume
        self.target_host = target_host

    def get(self, operation_id):
        facts = self.read_release_facts(operation_id)
        if not facts:
            raise LookupError('Target-local release not registered')
        first = facts[0]
        if (any(fact != first for fact in facts) or first.operation_id != operation_id
                or first.fact_kind != vc.FactKind.RELEASE or not isinstance(first.evidence, vc.PackageManifest)):
            raise ValueError('Ambiguous immutable release facts')
        package_digest = first.evidence.package_digest
        return PinnedRelease(first.release_id, vc.PackageRef(package_digest,
            f'{self.outbound_volume}/sha256/{package_digest}/manifest.json'), first.binding, self.target_host)


def fact_for(request, actor, kind, evidence, sequence=0, **lineage):
    fact = vc.OperationFact(event_id=request.identity.operation_id, fact_kind=kind,
        operation_id=request.identity.operation_id, transition_sequence=sequence, event_key='',
        binding=request.binding, request=request.identity, operation_type='promotion', requester_id=actor.subject_id,
        actor=actor, status=vc.FactStatus.REQUESTED, evidence=evidence, recorded_at=datetime.now(timezone.utc),
        release_id=request.identity.operation_id, **lineage)
    key = fact_key(fact)
    return replace(fact, event_id=str(uuid5(NAMESPACE_URL, key)), event_key=key)


class ReleaseCommands:
    def __init__(self, *, promotion, request_writer, authorize_history):
        self.promotion = promotion
        self.request_writer = request_writer
        self.authorize_history = authorize_history

    def create(self, version_id, mapping, policy, expected_base, source_profile, target_profile, key, actor):
        service = self.promotion
        self._enabled()
        self._scope(mapping.target_binding, actor)
        if (not key or source_profile.upper() == 'DEFAULT' or target_profile.upper() == 'DEFAULT'
                or source_profile != service.source_selection.profile or target_profile != service.target_selection.profile):
            raise PermissionError('Explicit server-bound source and target profiles required')
        service._executor(service.identity.executor(service.target_selection))
        service.transport.verify()
        reference = service.package(version_id, mapping, policy)
        manifest = vc.from_wire(vc.PackageManifest, json.loads(service.store.read(reference.manifest_uri)))
        artifact = json.loads(service.store.read(reference.manifest_uri.replace('manifest.json', 'artifact.json')))
        rendered = service.transformer.render(artifact, mapping, mapping.target_binding)
        operation_id = str(uuid5(NAMESPACE_URL, vc.canonical_json_hash('vc-release-id/1', {
            'workspace': actor.workspace_id, 'actor': actor.subject_id, 'key': key})))
        request = vc.MutationRequest(vc.RequestIdentity(operation_id, key, '0' * 64), mapping.target_binding,
            'promotion', version_id, expected_base, rendered.serialized_space, rendered.description, operation_id)
        request = replace(request, identity=replace(request.identity, request_digest=request_digest(request)))
        fact = fact_for(request, actor, vc.FactKind.RELEASE, manifest)
        self.request_writer(request, fact)
        return vc.OperationHandle(operation_id, vc.OperationStatus.REQUESTED, None)

    def validate(self, release_id, key, actor):
        self._enabled()
        operation = self._operation(release_id, actor, key)
        service = self.promotion
        evidence = service.preflight(release_id, service.identity.executor(service.target_selection))
        stage = vc.StageEvidence('VC/1.0', vc.PatchStage.CONFIG_OBSERVED, evidence.recorded_at,
                                 evidence.preflight_evidence_digest, None)
        service.facts.append(fact_for(operation.request, actor, vc.FactKind.OPERATION, stage))
        return vc.OperationHandle(release_id, vc.OperationStatus.REQUESTED, None)

    def promote(self, release_id, approval_id, key, actor):
        self._enabled()
        operation = self._operation(release_id, actor, key)
        if (operation.approval is None or operation.approval.status != vc.FactStatus.APPROVED
                or operation.request.approval_id != approval_id):
            raise PermissionError('Target approval required before an executable request')
        service = self.promotion
        executor = service.identity.executor(service.target_selection)
        evidence = service.preflight(release_id, executor)
        if evidence.preflight_evidence_digest != operation.approval.request.inputs.preflight_evidence_digest:
            raise ValueError('Preflight changed since approval')
        service.approvals.authorize(operation.request, executor)
        stage = vc.StageEvidence('VC/1.0', vc.PatchStage.CONFIG_PENDING, evidence.recorded_at,
                                 evidence.preflight_evidence_digest, None)
        service.facts.append(fact_for(operation.request, actor, vc.FactKind.OPERATION, stage, 1,
            approval_id=approval_id, approval_digest=operation.approval.approval_digest,
            approval_reference=ADMISSION_REFERENCE))
        return vc.OperationHandle(release_id, vc.OperationStatus.REQUESTED, None)

    def receipt(self, release_id, actor):
        operation = self.promotion.facts.get_request(release_id)
        binding = operation.request.binding
        if actor.workspace_id != binding.workspace_id or self.authorize_history(actor, binding) is not True:
            raise PermissionError('Receipt read scope denied')
        return receipts.existing(self.promotion.facts.lookup_request(binding, operation.request.identity.idempotency_key))

    def _enabled(self):
        if self.promotion.flags.enabled('vc_promotion_enabled') is not True:
            raise PermissionError('Promotion commands disabled')

    def _scope(self, binding, actor):
        service = self.promotion
        if (binding.workspace_id != service.workspace_id or actor.workspace_id != binding.workspace_id
                or service.identity.can_edit(actor.subject_id, binding) is not True
                or service.registry.resolve(binding.binding_id) != binding or binding.space_id is None):
            raise PermissionError('Pre-enrolled target-local release scope required')

    def _operation(self, release_id, actor, key):
        operation = self.promotion.facts.get_request(release_id)
        self._scope(operation.request.binding, actor)
        if operation.request.identity.operation_id != release_id or operation.request.identity.idempotency_key != key:
            raise ValueError('Release command identity mismatch')
        return operation
