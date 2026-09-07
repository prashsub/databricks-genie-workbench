"""Content-addressed portable inputs; consumers independently verify bytes."""

import json
from hashlib import sha256

from backend.services.version_control import contracts as vc


def encode(value):
    return json.dumps(vc.to_wire(value), sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return sha256(encode(value)).hexdigest()


def mapping_digest(mapping):
    return vc.canonical_json_hash('vc-mapping/1', vc.to_wire(mapping), digest_field='mapping_digest')


def policy_digests(policy):
    return (digest(policy.thresholds.get('validation', {})),
            digest(policy.thresholds.get('benchmark', {})))


def portable(snapshot):
    content = vc.to_wire(snapshot.serialized_space)
    for key in ('workspace_id', 'space_id', 'warehouse_id', 'parent_path', 'folder', 'permissions'):
        content.pop(key, None)
    return {'serialized_space': content, 'description': snapshot.restorable_metadata.get('description')}


def build(version, mapping, policy):
    artifact = portable(version.snapshot)
    artifact_bytes = encode(artifact)
    files = {'artifact.json': artifact_bytes, 'mapping.json': encode(mapping), 'validation.json': encode(policy)}
    validation, benchmark = policy_digests(policy)
    sources = artifact['serialized_space'].get('data_sources', {})
    manifest = dict(schema_version='VC/1.0', space_key=version.context.binding.space_key,
        source_version_id=version.version_id, raw_source_digest=version.snapshot.raw_state_digest,
        source_fingerprints=vc.to_wire(version.snapshot.fingerprints),
        artifact_digest=sha256(artifact_bytes).hexdigest(), mapping_digest=mapping_digest(mapping),
        transformer_version=mapping.transformer_version,
        required_sources=sorted({entry['identifier'] for kind in ('tables', 'metric_views')
                                 for entry in sources.get(kind, [])}),
        validation_policy_digest=validation, benchmark_policy_digest=benchmark,
        ownership={'space_key': version.context.binding.space_key},
        file_digests={name: sha256(content).hexdigest() for name, content in files.items()})
    manifest['package_digest'] = vc.canonical_json_hash('vc-package/1', manifest)
    return vc.from_wire(vc.PackageManifest, manifest), files
