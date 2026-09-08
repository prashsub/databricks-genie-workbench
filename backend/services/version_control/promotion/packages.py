"""Content-addressed portable inputs; consumers independently verify bytes."""

import json
from hashlib import sha256

from backend.services.version_control import contracts as vc
from backend.services.version_control.promotion.mapping import ENVIRONMENT_BINDING_KEYS, identifier_resources


def encode(value):
    return json.dumps(vc.to_wire(value), sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return sha256(encode(value)).hexdigest()


def immutable_put(store, path, content):
    store.put_if_absent(path, content)
    if store.read(path) != content:
        raise ValueError('Immutable artifact collision or incomplete upload')


def mapping_digest(mapping):
    return vc.canonical_json_hash('vc-mapping/1', vc.to_wire(mapping), digest_field='mapping_digest')


def policy_digests(policy):
    return (digest(policy.thresholds.get('validation', {})),
            digest(policy.thresholds.get('benchmark', {})))


def portable(snapshot):
    def strip_environment(value):
        if isinstance(value, dict):
            return {key: strip_environment(child) for key, child in value.items()
                    if key not in ENVIRONMENT_BINDING_KEYS}
        if isinstance(value, list):
            return [strip_environment(child) for child in value]
        return value

    content = strip_environment(vc.to_wire(snapshot.serialized_space))
    return {'serialized_space': content, 'description': snapshot.restorable_metadata.get('description')}


def build(version, mapping, policy):
    artifact = portable(version.snapshot)
    artifact_bytes = encode(artifact)
    files = {'artifact.json': artifact_bytes, 'mapping.json': encode(mapping), 'validation.json': encode(policy)}
    validation, benchmark = policy_digests(policy)
    manifest = dict(schema_version='VC/1.0', space_key=version.context.binding.space_key,
        source_version_id=version.version_id, raw_source_digest=version.snapshot.raw_state_digest,
        source_fingerprints=vc.to_wire(version.snapshot.fingerprints),
        artifact_digest=sha256(artifact_bytes).hexdigest(), mapping_digest=mapping_digest(mapping),
        transformer_version=mapping.transformer_version,
        required_sources=sorted({identifier for _, identifier
                                 in identifier_resources(artifact['serialized_space'])}),
        validation_policy_digest=validation, benchmark_policy_digest=benchmark,
        ownership={'space_key': version.context.binding.space_key},
        file_digests={name: sha256(content).hexdigest() for name, content in files.items()})
    manifest['package_digest'] = vc.canonical_json_hash('vc-package/1', manifest)
    return vc.from_wire(vc.PackageManifest, manifest), files
