"""Real OAuth principals and two workspaces; unavailable infrastructure is a blocker."""

import json
import os
from pathlib import Path
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
import re
import time
from uuid import UUID, uuid4

import pytest


pytestmark = pytest.mark.integration


class TwoWorkspacePromotion:
    """Run only against an operator-prepared immutable target-approved release."""

    def __init__(self, config):
        from databricks.sdk import WorkspaceClient
        self.config = config
        self.clients = {}
        required = {'source', 'target', 'approval', 'source_owner', 'target_owner'}
        assert required <= set(config['roles'])
        for role in required:
            selection = config['roles'][role]
            assert selection['profile'] and selection['profile'].upper() != 'DEFAULT'
            assert all(selection.get(key) for key in ('host', 'workspace_id', 'principal_id'))
            client = WorkspaceClient(profile=selection['profile'], host=selection['host'], auth_type='oauth-m2m')
            assert client.config.auth_type == 'oauth-m2m', 'Long-lived PAT is not integration evidence'
            self.clients[role] = client
        self.operation_id = str(UUID(config['operation_id']))
        assert self.operation_id == config['operation_id']
        self.probe_name = 'm07-permission-probe-' + str(uuid4())

    def verify_identities_and_securables(self):
        from backend.services.version_control.promotion.transport import separate_volumes
        for role, client in self.clients.items():
            selected = self.config['roles'][role]
            actual = client.api_client.do('GET', '/api/2.0/preview/scim/v2/Me',
                                          response_headers=['X-Databricks-Org-Id'])
            assert client.config.host.rstrip('/') == selected['host'].rstrip('/')
            assert str(actual['X-Databricks-Org-Id']) == selected['workspace_id']
            assert (actual.get('applicationId') or actual.get('userName')) == selected['principal_id']
        source = self.config['roles']['source']
        target = self.config['roles']['target']
        assert source['workspace_id'] != target['workspace_id']
        runtime_ids = {self.config['roles'][role]['principal_id'] for role in ('source', 'target', 'approval')}
        assert len(runtime_ids) == 3
        assert all(self.config['roles'][role]['principal_id'] not in runtime_ids
                   for role in ('source_owner', 'target_owner'))
        volumes = self.config['volumes']
        separate_volumes(volumes['outbound'], volumes['approval'], volumes['receipts'])
        for name, role in (('outbound', 'source_owner'), ('approval', 'target_owner'), ('receipts', 'target_owner')):
            full_name = '.'.join(volumes[name].split('/')[2:])
            details = self.clients[role].api_client.do('GET', '/api/2.1/unity-catalog/volumes/' + full_name)
            assert details['full_name'] == full_name
            assert details['owner'] not in runtime_ids
        assert self.config.get('local_acceptance_passed') is True, 'Local failure matrix remains a deployment gate'

    def verify_negative_permissions(self):
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.errors import PermissionDenied
        source = self.clients['source']
        target = self.clients['target']
        volumes = self.config['volumes']
        source.files.upload(volumes['outbound'] + '/' + self.probe_name, BytesIO(b'source-only'), overwrite=False)
        target.files.upload(volumes['receipts'] + '/' + self.probe_name, BytesIO(b'target-only'), overwrite=False)
        self.clients['approval'].files.upload(volumes['approval'] + '/' + self.probe_name,
                                             BytesIO(b'approval-only'), overwrite=False)
        for path in (volumes['approval'], self.config['reverse_receipt_volume']):
            with pytest.raises(PermissionDenied):
                source.files.upload(path + '/' + self.probe_name + '-denied', BytesIO(b'denied'), overwrite=False)
        with pytest.raises(PermissionDenied):
            target.files.upload(volumes['outbound'] + '/' + self.probe_name + '-denied', BytesIO(b'denied'), overwrite=False)
        source_at_target = WorkspaceClient(profile=self.config['roles']['source']['profile'],
            host=self.config['roles']['target']['host'], auth_type='oauth-m2m')
        binding = self.config['target_binding']
        before = target.api_client.do('GET', f"/api/2.0/genie/spaces/{binding['space_id']}")
        with pytest.raises(PermissionDenied):
            source_at_target.api_client.do('PATCH', f"/api/2.0/genie/spaces/{binding['space_id']}",
                                           body={'description': before.get('description', '')})
        namespace = self.config['target_namespace']
        assert re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*', namespace)
        response = self.sql(self.clients['source_owner'],
            f'SHOW GRANTS `{namespace.split(".")[0]}`.`{namespace.split(".")[1]}`.`genie_space_operations`')
        source_ids = {self.config['roles']['source']['principal_id'], *self.config.get('source_groups', [])}
        assert not any(row[0] in source_ids and row[1].upper() in {'MODIFY', 'INSERT', 'UPDATE', 'ALL PRIVILEGES'}
                       for row in response), 'Source has target operation-write authority'

    def sql(self, client, statement, parameters=None):
        response = client.api_client.do('POST', '/api/2.0/sql/statements', body={
            'warehouse_id': self.config['target_warehouse_id'], 'statement': statement,
            'parameters': parameters or [], 'wait_timeout': '10s', 'on_wait_timeout': 'CONTINUE'})
        deadline = time.monotonic() + 180
        while response['status']['state'] in {'PENDING', 'RUNNING'}:
            assert time.monotonic() < deadline, 'SQL probe timeout is not a passing capability test'
            time.sleep(2)
            response = client.api_client.do('GET', '/api/2.0/sql/statements/' + response['statement_id'])
        assert response['status']['state'] == 'SUCCEEDED', response['status']
        assert response.get('manifest', {}).get('truncated') is not True
        assert response.get('result', {}).get('next_chunk_index') is None, 'Bounded acceptance query must fit one chunk'
        return response.get('result', {}).get('data_array', [])

    def facts(self):
        namespace = self.config['target_namespace']
        assert re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*', namespace)
        catalog, schema = namespace.split('.')
        rows = self.sql(self.clients['target'],
            f'SELECT evidence_json FROM `{catalog}`.`{schema}`.`genie_space_operations` WHERE operation_id = :operation_id',
            [{'name': 'operation_id', 'value': self.operation_id, 'type': 'STRING'}])
        return [json.loads(row[0])['fact'] for row in rows]

    def promote_and_verify_retry_and_reverse_receipt(self):
        from backend.services.config_fingerprint import Canonicalizer
        from backend.services.version_control import contracts as vc
        from backend.services.version_control.promotion.mapping import MappingTransformer
        from backend.services.version_control.promotion.packages import digest, encode
        source = self.clients['source']
        target = self.clients['target']
        manifest_uri = self.config['manifest_uri']
        manifest_bytes = target.files.download(manifest_uri).contents.read()
        assert manifest_bytes == source.files.download(manifest_uri).contents.read()
        manifest = vc.from_wire(vc.PackageManifest, json.loads(manifest_bytes))
        assert manifest.source_version_id == self.config['source_version_id']
        assert vc.canonical_json_hash('vc-package/1', vc.to_wire(manifest), digest_field='package_digest') == manifest.package_digest
        prefix = manifest_uri.rsplit('/', 1)[0]
        files = {}
        for name, expected in manifest.file_digests.items():
            content = target.files.download(prefix + '/' + name).contents.read()
            assert sha256(content).hexdigest() == expected
            files[name] = json.loads(content)
        binding = vc.from_wire(vc.BindingRef, self.config['target_binding'])
        mapping = vc.from_wire(vc.MappingSpec, files['mapping.json'])
        rendered = MappingTransformer().render(files['artifact.json'], mapping, binding)
        canonicalizer = Canonicalizer()
        expected = canonicalizer.observe({'serialized_space': rendered.serialized_space, 'description': rendered.description})
        before = canonicalizer.observe(target.api_client.do('GET', f'/api/2.0/genie/spaces/{binding.space_id}',
                                                            query={'include_serialized_space': 'true'}))
        assert canonicalizer.compare(before, expected) == vc.Comparison.DIFFERENT, 'Acceptance must exercise a real target write'
        first = target.jobs.run_now(job_id=self.config['promotion_job_id'], job_parameters={'operation_id': self.operation_id},
            idempotency_token=sha256(('m07:first:' + self.operation_id).encode()).hexdigest()).result(timeout=timedelta(minutes=20))
        assert first.state.result_state.value == 'SUCCESS'
        facts = self.facts()
        receipts = [vc.from_wire(vc.DeploymentReceipt, fact['evidence']) for fact in facts if fact['fact_kind'] == 'receipt']
        assert receipts and all(receipt == receipts[0] for receipt in receipts)
        receipt = receipts[0]
        assert receipt.status == vc.OperationStatus.CONFIRMED and receipt.target_binding == binding
        assert receipt.package_digest == manifest.package_digest and receipt.mapping_digest == manifest.mapping_digest
        assert receipt.pre_version_id and receipt.post_version_id and receipt.pre_version_id != receipt.post_version_id
        actual = canonicalizer.observe(target.api_client.do('GET', f'/api/2.0/genie/spaces/{binding.space_id}',
                                                            query={'include_serialized_space': 'true'}))
        assert receipt.rendered_fingerprints == receipt.observed_fingerprints == actual.fingerprints == expected.fingerprints
        replay = target.jobs.run_now(job_id=self.config['promotion_job_id'], job_parameters={'operation_id': self.operation_id},
            idempotency_token=sha256(('m07:retry:' + self.operation_id).encode()).hexdigest()).result(timeout=timedelta(minutes=20))
        assert replay.state.result_state.value == 'SUCCESS'
        assert sorted(encode(fact) for fact in self.facts()) == sorted(encode(fact) for fact in facts)
        receipt_suffix = '/sha256/' + digest(receipt) + '/receipt.json'
        assert target.files.download(self.config['volumes']['receipts'] + receipt_suffix).contents.read() == encode(receipt)
        assert source.files.download(self.config['reverse_receipt_volume'] + receipt_suffix).contents.read() == encode(receipt)


def test_target_writes_source_cannot_and_receipt_is_reverse_read_only():
    location = os.environ.get('VC_TWO_WORKSPACE_CONFIG')
    assert location, 'DEPLOYMENT BLOCKER: real two-workspace OAuth configuration is required'
    config = json.loads(Path(location).read_text())
    assert config.get('disposable_nonproduction') is True
    platform = TwoWorkspacePromotion(config)
    platform.verify_identities_and_securables()
    platform.verify_negative_permissions()
    platform.promote_and_verify_retry_and_reverse_receipt()
