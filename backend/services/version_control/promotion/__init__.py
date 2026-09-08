"""Target-local cross-workspace promotion."""

from datetime import datetime, timezone
from hashlib import sha256
import json

from backend.services.version_control import contracts as vc
from backend.services.version_control.platform.identity import canonical_host
from .packages import build, digest, encode, immutable_put, mapping_digest, policy_digests, portable
from .mapping import identifier_resources
from . import receipts
from .transport import separate_volumes


class PromotionService:
    def __init__(self, **ports):
        self.__dict__.update(ports)

    def dispatch_pending(self, target_workspace_id, cursor=None):
        if self.flags.enabled('vc_promotion_enabled') is not True or target_workspace_id != self.workspace_id:
            raise PermissionError('Only enabled target-local dispatch is allowed')
        executor = self._executor(self.identity.executor(self.target_selection))
        candidates = self.pending.scan(target_workspace_id, cursor)
        handles = []
        for candidate in candidates.items:
            if candidate.status != vc.OperationStatus.REQUESTED:
                continue
            operation = self.facts.get_request(candidate.operation_id)
            request = operation.request
            if (not isinstance(request, vc.MutationRequest) or request.operation_type != 'promotion'
                    or request.binding.workspace_id != target_workspace_id
                    or request.identity.operation_id != candidate.operation_id or operation.approval is None
                    or operation.approval.status != vc.FactStatus.APPROVED):
                continue
            try:
                self.approvals.authorize(request, executor)
            except PermissionError:
                continue
            handles.append(self.dispatcher.submit_local(candidate.operation_id, 'promotion'))
        return vc.DispatchPage(tuple(handles), candidates.next_cursor)

    def execute(self, operation_id, executor):
        if self.flags.enabled('vc_promotion_enabled') is not True:
            raise PermissionError('Promotion writes disabled')
        executor = self._executor(executor)
        separate_volumes(self.outbound_volume, self.approval_volume, self.receipt_volume)
        operation = self.facts.get_request(operation_id)
        request = operation.request
        history = self.facts.lookup_request(request.binding, request.identity.idempotency_key)
        if history.ambiguous or any(fact.request != request.identity for fact in history.facts):
            raise ValueError('Ambiguous or reused promotion idempotency key')
        receipt = receipts.existing(history)
        if receipt is not None:
            if receipt.target_binding != request.binding or request.binding.workspace_id != self.workspace_id:
                raise PermissionError('Receipt repair must remain target-local')
            return receipts.export(self, receipt)
        attempted = any(fact.fact_kind == vc.FactKind.OPERATION and fact.status != vc.FactStatus.REQUESTED
                        for fact in history.facts)
        if attempted:
            if request.binding.workspace_id != self.workspace_id:
                raise PermissionError('Retry must remain target-local')
            return self.gate.verify_only(operation_id, executor)
        release, manifest, mapping, policy, artifact, rendered = self._inputs(operation_id)
        if operation.approval is None:
            raise PermissionError('Target approval required')
        inputs = operation.approval.request.inputs
        rendered_digest = vc.canonical_json_hash('vc-rendered-target/1', {
            'serialized_space': rendered.serialized_space, 'description': rendered.description})
        request_payload = vc.to_wire(request)
        request_payload['identity'] = {'operation_id': operation_id}
        expected = {
            'operation_id': operation_id, 'operation_type': 'promotion',
            'source_version_id': manifest.source_version_id, 'raw_source_digest': manifest.raw_source_digest,
            'source_fingerprints': manifest.source_fingerprints, 'artifact_digest': digest(artifact),
            'mapping_digest': mapping_digest(mapping), 'rendered_target_digest': rendered_digest,
            'transformer_version': self.transformer.version, 'canonicalizer_version': 'vc-c14n/1',
            'target_binding': release.target_binding, 'validation_policy_digest': policy_digests(policy)[0],
            'benchmark_policy_digest': policy_digests(policy)[1], 'thresholds': policy.thresholds}
        if (any(getattr(inputs, key) != value for key, value in expected.items())
                or request.binding != release.target_binding or request.identity.operation_id != operation_id
                or request.source_version_id != manifest.source_version_id or request.operation_type != 'promotion'
                or request.expected_base != inputs.expected_base_fingerprints.state_digest
                or vc.to_wire(request.serialized_space) != rendered.serialized_space or request.description != rendered.description
                or request.identity.request_digest != vc.canonical_json_hash('vc-request/1', request_payload)):
            raise ValueError('Immutable approved promotion inputs changed')
        evidence = self.preflight(operation_id, executor)
        if (inputs.preflight_evidence_digest != evidence.preflight_evidence_digest
                or inputs.permission_policy_digest != evidence.permission_policy_digest):
            raise ValueError('Target preflight evidence changed')
        self.approvals.authorize(request, executor)
        result = self.gate.execute(request, executor)
        receipt = receipts.record(self, operation, release, manifest, rendered, policy, result, executor)
        return receipts.export(self, receipt)

    def _executor(self, executor):
        selection = self.target_selection
        if (selection.profile is not None and (not selection.profile.strip() or selection.profile.upper() == 'DEFAULT')):
            raise PermissionError('Explicit non-default target profile or verified OBO required')
        if (executor.workspace_id != self.workspace_id or canonical_host(executor.host) != self.target_host
                or selection.workspace_id != self.workspace_id or canonical_host(selection.host) != self.target_host
                or not executor.execution_ref.startswith('job/')):
            raise PermissionError('Promotion requires the explicitly selected target-local Job')
        verified = self.identity.executor(selection)
        if verified != executor:
            raise PermissionError('Unverified target executor identity')
        self.identity.verify_run_as(executor.execution_ref, executor.principal_id)
        return verified

    def preflight(self, operation_id, executor):
        if self.flags.enabled('vc_promotion_enabled') is not True:
            raise PermissionError('Promotion writes disabled')
        executor = self._executor(executor)
        request = self.facts.get_request(operation_id).request
        release, manifest, mapping, policy, artifact, rendered = self._inputs(operation_id)
        if (request.binding.workspace_id != self.workspace_id or release.target_binding != request.binding
                or canonical_host(release.target_host) != self.target_host):
            raise PermissionError('Wrong target release intent')
        if self.registry.resolve(request.binding.binding_id) != request.binding or not request.binding.space_id:
            raise ValueError('Target must already be enrolled at the reviewed binding revision')
        environment = rendered.environment
        if not all(environment.get(key) for key in ('warehouse_id', 'parent_path', 'consumers')):
            raise PermissionError('Explicit target warehouse, folder and consumers required')
        resources = [('warehouse', environment['warehouse_id']), ('folder', environment['parent_path'])]
        resources.extend(identifier_resources(rendered.serialized_space))
        checks = []
        for principal in (executor.principal_id, *environment['consumers']):
            for kind, identifier in resources:
                if self.dependencies.check(request.binding, executor, principal, kind, identifier) is not True:
                    raise PermissionError('Target dependency or permission denied')
                checks.append((principal, kind, identifier))
        results = self.tests.run(rendered, policy, executor)
        if any(results[kind].get('passed') is not True for kind in ('validation', 'benchmark')):
            raise ValueError('Preflight tests failed')
        snapshot = self.canonicalizer.observe({'serialized_space': rendered.serialized_space,
                                               'description': rendered.description})
        observation = self.observer.capture(request.binding, 'promotion_preflight', executor)
        if (observation.busy or observation.status.stale or observation.status.quarantined
                or observation.status.drift in {vc.DriftState.UNKNOWN, vc.DriftState.UNREACHABLE,
                                               vc.DriftState.APPLIED_UNVERIFIED, vc.DriftState.CONFLICTED}
                or observation.captured_version is None
                or observation.captured_version.fingerprints.state_digest != request.expected_base):
            raise ValueError('Target drift or unresolved observation blocks promotion')
        base = observation.captured_version.fingerprints
        evidence = dict(schema_version='VC/1.0', operation_id=operation_id, target_binding=request.binding,
            expected_base_fingerprints=base, rendered_fingerprints=snapshot.fingerprints,
            permission_policy_digest=digest(checks), validation_evidence_digest=digest(results['validation']),
            benchmark_evidence_digest=digest(results['benchmark']))
        return vc.PreflightEvidence(**evidence, preflight_evidence_digest=digest(evidence),
                                     recorded_at=datetime.now(timezone.utc))

    def _inputs(self, operation_id):
        self.transport.verify()
        separate_volumes(self.outbound_volume, self.approval_volume, self.receipt_volume)
        release = self.releases.get(operation_id)
        manifest = vc.from_wire(vc.PackageManifest, json.loads(self.store.read(release.package.manifest_uri)))
        if (vc.canonical_json_hash('vc-package/1', vc.to_wire(manifest), digest_field='package_digest')
                != release.package.package_digest or manifest.package_digest != release.package.package_digest):
            raise ValueError('Package digest mismatch')
        prefix = release.package.manifest_uri.rsplit('/', 1)[0]
        if set(manifest.file_digests) != {'artifact.json', 'mapping.json', 'validation.json'}:
            raise ValueError('Unexpected package file set')
        files = {}
        for name, expected in manifest.file_digests.items():
            content = self.store.read(prefix + '/' + name)
            if sha256(content).hexdigest() != expected:
                raise ValueError('Package file digest mismatch')
            files[name] = json.loads(content)
        artifact = files['artifact.json']
        mapping = vc.from_wire(vc.MappingSpec, files['mapping.json'])
        policy = vc.from_wire(vc.TestPolicy, files['validation.json'])
        source = self.ledger.get_version(self.source_binding, manifest.source_version_id)
        snapshot = self.canonicalizer.observe(vc.to_wire(source.snapshot.response_envelope))
        if (source.version_id != manifest.source_version_id or source.context.binding != self.source_binding
                or snapshot.raw_state_digest != manifest.raw_source_digest
                or snapshot.fingerprints != manifest.source_fingerprints or portable(snapshot) != artifact
                or digest(artifact) != manifest.artifact_digest or mapping_digest(mapping) != manifest.mapping_digest
                or mapping.transformer_version != manifest.transformer_version
                or policy_digests(policy) != (manifest.validation_policy_digest, manifest.benchmark_policy_digest)):
            raise ValueError('Recomputed source, artifact, mapping or policy digest mismatch')
        rendered = self.transformer.render(artifact, mapping, release.target_binding)
        return release, manifest, mapping, policy, artifact, rendered

    def package(self, version_id, mapping, policy):
        if self.flags.enabled('vc_promotion_enabled') is not True:
            raise PermissionError('Promotion writes disabled')
        selection = self.source_selection
        if selection.profile is not None and (not selection.profile.strip() or selection.profile.upper() == 'DEFAULT'):
            raise PermissionError('Explicit source profile or verified OBO required')
        executor = self.source_identity.executor(selection)
        if (executor.workspace_id != self.source_binding.workspace_id or executor.workspace_id != selection.workspace_id
                or canonical_host(executor.host) != canonical_host(selection.host)
                or executor.principal_id != selection.principal_id):
            raise PermissionError('Source identity does not match selected immutable source')
        separate_volumes(self.outbound_volume)
        version = self.ledger.get_version(self.source_binding, version_id)
        if version.version_id != version_id or version.context.binding != self.source_binding:
            raise ValueError('Immutable source binding/version mismatch')
        manifest, files = build(version, mapping, policy)
        prefix = f'{self.outbound_volume}/sha256/{manifest.package_digest}'
        for name, content in files.items():
            immutable_put(self.store, f'{prefix}/{name}', content)
        reference = vc.PackageRef(manifest.package_digest, f'{prefix}/manifest.json')
        immutable_put(self.store, reference.manifest_uri, encode(manifest))
        self.store.publish(reference)
        return reference
