"""Real-platform approval isolation, durable audit, identity, and outage probes.

These are DEPLOYMENT BLOCKERS, not passing tests: the local `platform` fixture
requires the real `m06_platform` fixture and FAILS explicitly when it is absent.
Like `live_platform` in this directory's conftest.py, it never silently skips an
unconfigured environment. No cross-directory conftest imports are needed.
Deselected by default; run only under the sanctioned gate:

    python -m pytest backend/tests/integration -m integration -q
"""

import pytest


@pytest.fixture
def platform(request):
    try:
        return request.getfixturevalue('m06_platform')
    except pytest.FixtureLookupError:
        pytest.fail('DEPLOYMENT BLOCKER: supply the real M08 m06_platform fixture; no platform in offline sandbox')


@pytest.mark.integration
@pytest.mark.parametrize('kind', ['approval_request', 'receipt'])
def test_untrusted_source_cannot_insert_target_approvals_or_receipts(platform, kind):
    statement, parameters = platform.valid_insert(kind)
    platform.target_sql(statement, parameters)
    with pytest.raises(PermissionError):
        platform.source_sql(statement, parameters)
    with pytest.raises(PermissionError):
        platform.source_sql(f"ALTER TABLE {platform.operations_table} SET TBLPROPERTIES ('delta.appendOnly'='false')", {})
    assert platform.read_approved_export_as_source()
    with pytest.raises(PermissionError):
        platform.read_private_evidence_as_source()


@pytest.mark.integration
def test_real_delta_append_only_and_durable_replay(platform):
    ledger = platform.operation_facts()
    original = platform.valid_fact()
    reference = ledger.append(original)
    assert platform.operation_facts().append(original) == reference
    assert platform.count_event(original.event_key) == 1
    with pytest.raises(PermissionError):
        platform.target_sql(f'DELETE FROM {platform.operations_table} WHERE event_key = :key', {'key': original.event_key})
    with pytest.raises(PermissionError):
        platform.target_sql(f'UPDATE {platform.operations_table} SET status = :status WHERE event_key = :key',
                            {'status': 'confirmed', 'key': original.event_key})


@pytest.mark.integration
def test_real_target_sp_membership_revocation_and_human_identity(platform):
    service, request, executor, human = platform.approved_production_request()
    platform.identity.verify_run_as(executor.execution_ref, executor.principal_id)
    assert platform.authenticated_human().actor_kind == 'human'
    assert platform.authenticated_service().actor_kind == 'service'
    assert service.authorize(request, executor).approval_id == request.approval_id
    with platform.revoke_target_approver_membership(human):
        with pytest.raises(PermissionError):
            service.authorize(request, executor)


@pytest.mark.integration
def test_real_delta_and_evidence_volume_outage_disable_authorization(platform):
    service, request, executor, _ = platform.approved_production_request()
    with platform.deny_operation_reads():
        with pytest.raises((PermissionError, OSError)):
            service.authorize(request, executor)
    with platform.deny_private_evidence_reads():
        with pytest.raises((PermissionError, OSError)):
            service.authorize(request, executor)
