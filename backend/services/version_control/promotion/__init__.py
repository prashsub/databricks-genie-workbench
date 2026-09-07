"""Target-local cross-workspace promotion."""

from datetime import datetime, timezone
import json

from backend.services.version_control import contracts as vc
from .packages import build, digest, encode, immutable_put


class PromotionService:
    def __init__(self, **ports):
        self.__dict__.update(ports)

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
        base = self.observer.capture(request.binding, 'promotion_preflight', executor).snapshot.fingerprints
        evidence = dict(schema_version='VC/1.0', operation_id=operation_id, target_binding=request.binding,
            expected_base_fingerprints=base, rendered_fingerprints=snapshot.fingerprints,
            permission_policy_digest=digest(checks), validation_evidence_digest=digest(results['validation']),
            benchmark_evidence_digest=digest(results['benchmark']))
        return vc.PreflightEvidence(**evidence, preflight_evidence_digest=digest(evidence),
                                     recorded_at=datetime.now(timezone.utc))

    def _inputs(self, operation_id):
        release = self.releases.get(operation_id)
        manifest = vc.from_wire(vc.PackageManifest, json.loads(self.store.read(release.package.manifest_uri)))
        prefix = release.package.manifest_uri.rsplit('/', 1)[0]
        artifact = json.loads(self.store.read(prefix + '/artifact.json'))
        mapping = vc.from_wire(vc.MappingSpec, json.loads(self.store.read(prefix + '/mapping.json')))
        policy = vc.from_wire(vc.TestPolicy, json.loads(self.store.read(prefix + '/validation.json')))
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
