"""M07 immutable package contract tests."""

import json
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from backend.services.config_fingerprint import Canonicalizer
from backend.services.version_control import contracts as vc
from backend.services.version_control.platform.feature_flags import FeatureFlags
from backend.services.version_control.promotion import PromotionService


def uid(number):
    return str(UUID(int=number))


class MemoryFiles:
    def __init__(self):
        self.files = {}
        self.published = []

    def read(self, path):
        return self.files[path]

    def put_if_absent(self, path, content):
        self.files.setdefault(path, content)

    def publish(self, reference):
        self.published.append(reference)


@pytest.fixture
def package_rig():
    source = vc.BindingRef(uid(1), 1, 'sales', 'source', 'source-space', 'dev')
    target = vc.BindingRef(uid(2), 1, 'sales', 'target', 'target-space', 'prod')
    canonicalizer = Canonicalizer()
    snapshot = canonicalizer.observe({'space_id': 'source-space', 'workspace_id': 'source',
        'warehouse_id': 'source-warehouse', 'parent_path': '/source/folder',
        'serialized_space': {'version': 2, 'data_sources': {'tables': [{'identifier': 'dev.sales.orders'}]}},
        'description': 'Sales'})
    version = vc.Version(uid(3), snapshot, vc.CaptureContext(source, 'capture',
        datetime.now(timezone.utc), 'initial', vc.ActorContext('author', 'source', 'user'), vc.Origin.WORKBENCH))
    ledger = Mock(spec=vc.VersionLedger)
    ledger.get_version.return_value = version
    mapping = vc.MappingSpec('VC/1.0', target, {'dev.sales.orders': 'prod.sales.orders'}, '0' * 64, 'vc-map/1')
    policy = vc.TestPolicy('VC/1.0', '0' * 64, '0' * 64, {'validation': {'smoke': True}, 'benchmark': {'minimum': 1}})
    store = MemoryFiles()
    service = PromotionService(ledger=ledger, source_binding=source, store=store,
        outbound_volume='/Volumes/control/vc/vc_outbound_packages', canonicalizer=canonicalizer,
        flags=FeatureFlags(vc_writes_enabled=True, vc_promotion_enabled=True))
    service.source_selection = vc.ExplicitExecutorSelection('source', 'https://source.example.com', 'source-sp',
                                                           'job/source', 'source-profile')
    service.source_identity = Mock(spec=vc.IdentityProvider)
    service.source_identity.executor.return_value = vc.ExecutorContext('source', 'https://source.example.com',
        'source-sp', 'service_principal', object(), 'job/source')
    return SimpleNamespace(service=service, source=source, target=target, version=version,
        ledger=ledger, mapping=mapping, policy=policy, store=store)


def test_package_pins_immutable_source_and_excludes_source_environment_ids(package_rig):
    rig = package_rig
    reference = rig.service.package(rig.version.version_id, rig.mapping, rig.policy)
    manifest = json.loads(rig.store.read(reference.manifest_uri))
    prefix = reference.manifest_uri.rsplit('/', 1)[0]
    artifact = rig.store.read(prefix + '/artifact.json')
    assert manifest['source_version_id'] == rig.version.version_id
    assert manifest['raw_source_digest'] == rig.version.snapshot.raw_state_digest
    assert manifest['package_digest'] == reference.package_digest
    assert all(value not in artifact.decode() for value in ('source-space', 'source-warehouse', '/source/folder', 'workspace_id'))
    assert json.loads(artifact)['serialized_space']['data_sources']['tables'][0]['identifier'] == 'dev.sales.orders'
    for filename, digest in manifest['file_digests'].items():
        assert sha256(rig.store.read(prefix + '/' + filename)).hexdigest() == digest
    rig.ledger.get_version.assert_called_once_with(rig.source, rig.version.version_id)


@pytest.mark.parametrize('failure', ['collision', 'missing', 'equal'])
def test_existing_digest_path_is_never_overwritten_and_incomplete_package_not_published(package_rig, failure):
    rig = package_rig
    reference = rig.service.package(rig.version.version_id, rig.mapping, rig.policy)
    original = dict(rig.store.files)
    rig.store.published.clear()
    artifact_path = reference.manifest_uri.replace('manifest.json', 'artifact.json')
    if failure == 'equal':
        assert rig.service.package(rig.version.version_id, rig.mapping, rig.policy) == reference
        assert rig.store.files == original
    else:
        rig.store.files.pop(reference.manifest_uri)
        if failure == 'collision':
            rig.store.files[artifact_path] = b'substituted'
        else:
            rig.store.files.pop(artifact_path)
            rig.store.put_if_absent = Mock()
        with pytest.raises((ValueError, KeyError)):
            rig.service.package(rig.version.version_id, rig.mapping, rig.policy)
        assert reference.manifest_uri not in rig.store.files
        assert not rig.store.published
        if failure == 'collision':
            assert rig.store.files[artifact_path] == b'substituted'


def test_package_rejects_implicit_source_profile(package_rig):
    from dataclasses import replace
    rig = package_rig
    rig.service.source_selection = replace(rig.service.source_selection, profile='DEFAULT')
    with pytest.raises(PermissionError):
        rig.service.package(rig.version.version_id, rig.mapping, rig.policy)
    assert not rig.store.files
