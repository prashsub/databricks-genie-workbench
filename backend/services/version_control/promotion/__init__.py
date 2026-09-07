"""Target-local cross-workspace promotion."""

from datetime import datetime, timezone
from hashlib import sha256
import json

from backend.services.version_control import contracts as vc
from .packages import build, digest, encode, immutable_put, mapping_digest, policy_digests, portable


class PromotionService:
    def __init__(self, **ports):
        self.__dict__.update(ports)

    def execute(self, operation_id, executor):
        if self.flags.enabled('vc_promotion_enabled') is not True:
            raise PermissionError('Promotion writes disabled')
        operation = self.facts.get_request(operation_id)
        request = operation.request
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
        return self.gate.execute(request, executor)

    def preflight(self, operation_id, executor):
        request = self.facts.get_request(operation_id).request
        release, manifest, mapping, policy, artifact, rendered = self._inputs(operation_id)
        environment = rendered.environment
        if not all(environment.get(key) for key in ('warehouse_id', 'parent_path', 'consumers')):
            raise PermissionError('Explicit target warehouse, folder and consumers required')
        resources = [('warehouse', environment['warehouse_id']), ('folder', environment['parent_path'])]
        sources = rendered.serialized_space.get('data_sources', {})
        for collection, kind in (('tables', 'table'), ('metric_views', 'metric_view'),
                                 ('catalogs', 'catalog'), ('schemas', 'schema')):
            resources.extend((kind, entry['identifier']) for entry in sources.get(collection, []))
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
        base = self.observer.capture(request.binding, 'promotion_preflight', executor).captured_version.fingerprints
        evidence = dict(schema_version='VC/1.0', operation_id=operation_id, target_binding=request.binding,
            expected_base_fingerprints=base, rendered_fingerprints=snapshot.fingerprints,
            permission_policy_digest=digest(checks), validation_evidence_digest=digest(results['validation']),
            benchmark_evidence_digest=digest(results['benchmark']))
        return vc.PreflightEvidence(**evidence, preflight_evidence_digest=digest(evidence),
                                     recorded_at=datetime.now(timezone.utc))

    def _inputs(self, operation_id):
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
