"""M03 task 16: REAL Delta CAS release gate, not a sandbox-green test.

Deployment-only setup (no default credentials, workspace, or production schema):
* Install databricks-sql-connector >=4,<5 in the deployment test environment.
* Set VC_DELTA_CAS_NONPRODUCTION=YES, VC_DELTA_CAS_CATALOG,
  VC_DELTA_CAS_SCHEMA (must start with vc_m03_test_), VC_DELTA_CAS_HOST
  (hostname only), VC_DELTA_CAS_HTTP_PATH, and VC_DELTA_CAS_WORKSPACE_ID.
* Set VC_DELTA_CAS_OWNER_PROFILE, VC_DELTA_CAS_EXECUTOR_A_PROFILE, and
  VC_DELTA_CAS_EXECUTOR_B_PROFILE to explicit SDK profiles. A/B must be distinct
  service principals; the same-SP case opens A twice. Owner must be distinct.
  Profiles contain credentials outside the repository. Never print credentials.
* The owner provisions disposable tables in the explicitly designated existing
  schema and grants SELECT/MODIFY on coordination and artifact tables (UC has
  no INSERT/UPDATE privilege; append-only is enforced by delta.appendOnly).
  Compute must support these fine-grained grants; no broad MODIFY fallback.

Run: python -m pytest backend/tests/integration/test_vc_delta_cas.py -m integration
Missing configuration/dependencies FAIL, not skip. Collection needs neither.
Only uniquely named tables created here are dropped, only by the owner.

The test-local SQL adapter implements M03's actual read/compare_and_swap port:
UPDATE-only MERGE, Serializable, exact read-back, no acquisition retry. This
branch has no M08 production SQL adapter. Persisted JSON artifacts implement the
minimal fact/evidence ports, NOT M02/M06 production storage acceptance. A durable
send-boundary probe is the mutation trace: this CAS test never PATCHes Genie.
Production adapter wiring, real Genie transport, and full effective-grant tests
remain separate deployment gates. No local run proves any real-Delta behavior.
"""
import json
import os
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.services.version_control import contracts as c
from backend.services.version_control.coordination import (
    CoordinationError,
    CoordinationService,
)
from backend.services.version_control.coordination.row import CoordinationRow
from backend.services.version_control.coordination.service import OwnershipError

pytestmark = pytest.mark.integration


def uid():
    return str(uuid4())


class Clock:
    def now(self):
        return datetime.now(UTC)


def execute(connection, statement, parameters=None):
    """One execution, no application-level retry or exception-as-success."""
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters or {})
        return [row.asDict() for row in cursor.fetchall()] if cursor.description else []


def dumps(value):
    """c.to_wire() yields frozen mappingproxy structures; json can't serialize
    those directly, so convert any Mapping to a plain dict on the way out."""
    def default(obj):
        if isinstance(obj, Mapping):
            return dict(obj)
        raise TypeError(f'Not JSON serializable: {type(obj).__name__}')
    return json.dumps(value, default=default)


def row_values(row):
    """Flatten the real M03 row into the owned coordination DDL columns."""
    values = {f.name: getattr(row, f.name) for f in fields(row)
              if f.name not in {'binding', 'heads', 'checkpoint', 'termination_evidence'}}
    values.update({f.name: getattr(row.binding, f.name) for f in fields(row.binding)
                   if f.name != 'environment'})
    for name in ('checkpoint', 'termination_evidence'):
        value = getattr(row, name)
        values[f'{name}_json'] = dumps(value) if value is not None else None
    for name in ('observed', 'approved', 'deployed'):
        values[f'{name}_head_version_id'] = getattr(row.heads, name)
    # SQL connector uses naive UTC TIMESTAMPs; never depend on host timezone.
    return {key: (value.astimezone(UTC).replace(tzinfo=None)
                  if isinstance(value, datetime) else value.value
                  if isinstance(value, (c.CoordinationState, c.PatchStage)) else value)
            for key, value in values.items()}


class DeltaCoordinationStore:
    """Real SQL implementation of the injected M03 store port, local to this gate."""
    def __init__(self, connection, table, binding):
        self.connection, self.table, self.binding = connection, table, binding
        self.barrier = None
        self.proposals = []

    def rows(self):
        return execute(self.connection, f'SELECT * FROM {self.table} WHERE binding_id = :id',
                       {'id': self.binding.binding_id})

    def read(self, binding_id):
        assert binding_id == self.binding.binding_id
        rows = self.rows()
        if len(rows) > 1:
            raise OwnershipError('Duplicate real Delta coordination rows')
        if not rows:
            return None
        values = dict(rows[0])
        binding_values = {f.name: values.pop(f.name) for f in fields(c.BindingRef)
                          if f.name != 'environment'}
        binding = c.BindingRef(**binding_values, environment=self.binding.environment)
        heads = c.Heads(*(values.pop(f'{name}_head_version_id')
                          for name in ('observed', 'approved', 'deployed')))
        for name in ('checkpoint', 'termination_evidence'):
            value = values.pop(f'{name}_json')
            values[name] = json.loads(value) if value is not None else None
        for key, value in values.items():
            if isinstance(value, datetime):
                values[key] = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        values['state'] = c.CoordinationState(values['state'])
        if values['mutation_stage'] is not None:
            values['mutation_stage'] = c.PatchStage(values['mutation_stage'])
        return CoordinationRow(binding=binding, heads=heads, **values)

    def compare_and_swap(self, binding_id, binding_revision, row_version,
                         expected_generation, predicate, **changes):
        # The fence generation is a separate positional; `changes` may carry the
        # NEW generation, so this parameter must not be named `generation`.
        before = self.read(binding_id)
        if (before is None or (before.binding.binding_revision, before.row_version,
                              before.generation) != (binding_revision, row_version, expected_generation)
                or not predicate(before)):
            return None
        after = replace(before, **changes, row_version=row_version + 1,
                        updated_at=Clock().now())
        values = row_values(after)
        parameters = {f'new_{key}': value for key, value in values.items()}
        parameters.update(id=binding_id, revision=binding_revision, version=row_version,
                          generation=expected_generation, state=before.state.value,
                          unresolved=before.unresolved, attempt=before.attempt_id)
        assignments = ', '.join(f't.{key} = :new_{key}' for key in values)
        # The Python predicate is checked on this exact version; EVERY runtime
        # update increments row_version. SQL rechecks the fence and occupied state.
        statement = f'''MERGE INTO {self.table} AS t
            USING (SELECT :id AS binding_id) AS s ON t.binding_id = s.binding_id
            WHEN MATCHED AND t.binding_revision = :revision
              AND t.row_version = :version AND t.generation = :generation
              AND t.state = :state AND t.unresolved = :unresolved
              AND t.attempt_id <=> :attempt
            THEN UPDATE SET {assignments}'''
        if self.barrier is not None:
            self.proposals.append((before, after))
            barrier, self.barrier = self.barrier, None  # reservation only, never admission
            barrier.wait(timeout=120)  # both have read the SAME existing idle version
        try:
            execute(self.connection, statement, parameters)
        except Exception as exc:
            # Only a confirmed Delta optimistic concurrency abort is a CAS loss.
            # Timeouts, permissions, unknown/ambiguous responses MUST fail the test.
            if re.search(r'\[DELTA_CONCURRENT_(?:APPEND|DELETE_READ|DELETE_DELETE|WRITE)(?:\.[A-Z_]+)?\]',
                         str(exc)):
                return None
            raise
        observed = self.read(binding_id)
        return observed if observed == after else None


class DeltaArtifacts:
    """Append-only REAL Delta evidence/facts/trace, no fake authority fallback."""
    def __init__(self, connection, table, binding):
        self.connection, self.table, self.binding = connection, table, binding

    def put(self, kind, key, value):
        execute(self.connection, f'INSERT INTO {self.table} VALUES (:kind, :key, :payload)',
                {'kind': kind, 'key': key, 'payload': dumps(c.to_wire(value))})

    def get(self, kind, key=None):
        clause = '' if key is None else ' AND artifact_key = :key'
        parameters = {'kind': kind} if key is None else {'kind': kind, 'key': key}
        rows = execute(self.connection,
                       f'SELECT payload FROM {self.table} WHERE kind = :kind{clause}', parameters)
        return [json.loads(row['payload']) for row in rows]

    def verify_committed(self, observation):
        return self.get('preimage', observation.version_id) == [c.to_wire(observation)]

    def append(self, fact):
        self.put('fact', fact.event_key, fact)
        assert self.get('fact', fact.event_key) == [c.to_wire(fact)]
        return c.FactRef(fact.event_id, fact.event_key,
                         c.canonical_json_hash('vc-cas-test-fact/1', c.to_wire(fact)))

    def lookup_request(self, binding, key):
        facts = tuple(c.from_wire(c.OperationFact, value) for value in self.get('fact'))
        matching = tuple(f for f in facts if f.binding == binding and f.request.idempotency_key == key)
        return c.RequestHistory(matching, len({f.event_key for f in matching}) != len(matching))

    def approval_use(self, binding, approval_id):
        # This CAS gate uses grants without approvals; approval reuse has its own gate.
        raise AssertionError('Approval-bearing flow not configured in this CAS test')

    def publish_consumption(self, claim):
        existing = self.get('consumption', claim.attempt_id)
        if not existing:
            self.put('consumption', claim.attempt_id, claim)
        assert self.verify_flush(claim)

    def verify_flush(self, claim):
        return self.get('consumption', claim.attempt_id) == [c.to_wire(claim)]


def deny(*args):
    return False


def forbidden(*args):
    raise AssertionError('Runtime enrollment/recovery is forbidden in this test')


def service(store, artifacts, binding, grant):
    return CoordinationService(
        store=store, facts=artifacts, ledger=artifacts, clock=Clock(),
        resolve_binding=lambda binding_id: binding if binding_id == binding.binding_id else None,
        verify_enrollment=deny, insert_enrolled=forbidden,
        validate_authorization=lambda candidate, executor: candidate == grant,
        verify_create_intent=deny, termination=SimpleNamespace(for_attempt=forbidden),
        authorize_human_recovery=deny, authorize_heads=deny,
    )


def required(name):
    value = os.environ.get(f'VC_DELTA_CAS_{name}')
    if not value:
        pytest.fail(f'Deployment BLOCKER: explicitly configure VC_DELTA_CAS_{name}', pytrace=False)
    return value


def identifier(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        pytest.fail('Deployment BLOCKER: catalog/schema must be simple SQL identifiers', pytrace=False)
    return f'`{value}`'


@pytest.fixture
def delta(request):
    if required('NONPRODUCTION') != 'YES':
        pytest.fail('Deployment BLOCKER: explicit nonproduction opt-in required', pytrace=False)
    catalog, schema = required('CATALOG'), required('SCHEMA')
    if not schema.startswith('vc_m03_test_'):
        pytest.fail('Refusing provisioning outside a vc_m03_test_* schema', pytrace=False)
    namespace = f'{identifier(catalog)}.{identifier(schema)}'
    host, http_path, workspace_id = required('HOST'), required('HTTP_PATH'), required('WORKSPACE_ID')
    profiles = [required(f'{role}_PROFILE') for role in ('OWNER', 'EXECUTOR_A', 'EXECUTOR_B')]
    try:
        from databricks import sql
        from databricks.sdk.core import Config
    except ImportError:
        pytest.fail('Deployment BLOCKER: install databricks-sql-connector >=4,<5', pytrace=False)

    same_sp = getattr(request, 'param', False)
    with ExitStack() as stack:
        def connect(profile):
            config = Config(profile=profile)
            assert config.host.rstrip('/') == f'https://{host}', 'Profile targets wrong workspace'
            connection = stack.enter_context(sql.connect(
                server_hostname=host, http_path=http_path,
                credentials_provider=lambda: config.authenticate,
                _retry_stop_after_attempts_count=1,
            ))
            # Serverless DBSQL rejects the spark.sql.session.timeZone session config
            # (CONFIG_NOT_AVAILABLE); SET TIME ZONE is accepted and pins the same UTC
            # guarantee the naive-TIMESTAMP handling depends on. Assert it took.
            execute(connection, "SET TIME ZONE 'UTC'")
            assert execute(connection, 'SELECT current_timezone() AS tz')[0]['tz'] == 'UTC'
            return connection

        owner = connect(profiles[0])
        clients = [connect(profiles[1]), connect(profiles[1] if same_sp else profiles[2])]
        assert clients[0] is not clients[1]  # independent SQL sessions, even with the same SP
        principals = [execute(client, 'SELECT current_user() AS principal')[0]['principal']
                      for client in clients]
        owner_principal = execute(owner, 'SELECT current_user() AS principal')[0]['principal']
        assert owner_principal not in principals, 'Enrollment owner must not be a runtime executor'
        assert (principals[0] == principals[1]) == same_sp
        suffix = uuid4().hex
        table, artifacts_table = (f'{namespace}.`vc_m03_{kind}_{suffix}`'
                                  for kind in ('coordination', 'artifacts'))
        ddl = (Path(__file__).resolve().parents[2] / 'version_control_ddl' / '05-coordination.sql').read_text()
        ddl = ddl.replace('${catalog}.${control_schema}.genie_ops_coordination', table)
        # The owned DDL is CREATE + idempotent ALTER ADD CONSTRAINT (inline CHECK is
        # unsupported); the SQL connector executes one statement per call, so split on
        # ';' and drop full-line comments. Register cleanup immediately after the CREATE
        # so a later ALTER failure still tears the table down. Runtime clients never
        # INSERT coordination, ALTER isolation, or perform destructive teardown.
        uncommented = '\n'.join(line for line in ddl.splitlines()
                                if not line.strip().startswith('--'))
        statements = [statement.strip() for statement in uncommented.split(';') if statement.strip()]
        execute(owner, statements[0])
        stack.callback(execute, owner, f'DROP TABLE {table}')
        for statement in statements[1:]:
            execute(owner, statement)
        execute(owner, f'''CREATE TABLE {artifacts_table}
            (kind STRING, artifact_key STRING, payload STRING) USING DELTA
            TBLPROPERTIES ('delta.appendOnly'='true')''')
        stack.callback(execute, owner, f'DROP TABLE {artifacts_table}')
        for principal in set(principals):
            grantee = '`' + principal.replace('`', '``') + '`'
            execute(owner, f'GRANT SELECT, MODIFY ON TABLE {table} TO {grantee}')
            execute(owner, f'GRANT SELECT, MODIFY ON TABLE {artifacts_table} TO {grantee}')
        for client in [owner, *clients]:
            detail = execute(client, f'DESCRIBE DETAIL {table}')
            assert len(detail) == 1 and detail[0]['format'].lower() == 'delta'
            # The connector returns the MAP<STRING,STRING> properties column as a
            # list of (key, value) tuples, not a dict.
            properties = dict(detail[0]['properties'])
            assert properties.get('delta.isolationLevel') == 'Serializable'

        binding = c.BindingRef(uid(), 1, f'cas-{suffix}', workspace_id,
                               f'disposable-cas-{suffix}', 'test')
        initial = CoordinationRow(binding, Clock().now())

        def seed(row):
            values = row_values(row)
            execute(owner, f'INSERT INTO {table} ({", ".join(values)}) VALUES '
                    f'({", ".join(":" + key for key in values)})', values)

        seed(initial)  # ONE pre-existing row; reserve must never insert
        observer = DeltaCoordinationStore(owner, table, binding)
        assert observer.read(binding.binding_id) == initial
        artifacts = DeltaArtifacts(owner, artifacts_table, binding)
        fingerprints = c.Fingerprints('1' * 64, '2' * 64, '3' * 64, 'vc-c14n/1')
        preimage = c.ObservationRef(uid(), binding.binding_id, 1,
                                   fingerprints.state_digest, '4' * 64)
        artifacts.put('preimage', preimage.version_id, preimage)
        assert artifacts.verify_committed(preimage)
        contenders = []
        for index, connection in enumerate(clients):
            operation = c.RequestIdentity(uid(), uid(), '5' * 64)
            grant = c.AuthorizationGrant(binding, operation, 'cas-test-policy', fingerprints,
                '6' * 64, Clock().now() + timedelta(hours=1), None, None)
            executor = c.ExecutorContext(workspace_id, f'https://{host}', principals[index],
                                          'service', None, f'cas-worker:{uid()}')
            store = DeltaCoordinationStore(connection, table, binding)
            ports = DeltaArtifacts(connection, artifacts_table, binding)
            contenders.append(SimpleNamespace(store=store, artifacts=ports, request=operation,
                grant=grant, executor=executor, service=service(store, ports, binding, grant)))
        def expire_lease(claim):
            # Owner-only fault injection, NOT runtime takeover or a local clock fake.
            execute(owner, f'''UPDATE {table}
                SET lease_expires_at = :expired, row_version = row_version + 1
                WHERE binding_id = :binding AND attempt_id = :attempt''',
                {'expired': (Clock().now() - timedelta(minutes=1)).replace(tzinfo=None),
                 'binding': binding.binding_id, 'attempt': claim.attempt_id})
            assert observer.read(binding.binding_id).lease_expires_at < Clock().now()

        yield SimpleNamespace(binding=binding, initial=initial, seed=seed, observer=observer,
                              artifacts=artifacts, preimage=preimage, contenders=contenders,
                              expire_lease=expire_lease)


def reserve(contender, binding):
    try:
        return contender.service.reserve(binding, contender.request, contender.executor)
    except OwnershipError as exc:
        return exc  # ONLY definite CAS/ownership losses, never outages or ambiguous SQL


def race(delta):
    barrier = Barrier(2)
    for contender in delta.contenders:
        contender.store.barrier = barrier
    reservations_done = Barrier(2)

    def run(contender):
        result = reserve(contender, delta.binding)
        # Both workers follow the SAME reserve -> admit -> send-boundary path.
        # Let the losing conflict commit before testing admission publication;
        # only the coordination-row CAS is the contested authority in this gate.
        reservations_done.wait(timeout=120)
        if isinstance(result, c.Reservation):
            return result, admit_and_trace(delta, contender, result)
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, delta.contenders))
    proposals = [contender.store.proposals for contender in delta.contenders]
    assert all(len(proposal) == 1 for proposal in proposals), 'No acquisition retry allowed'
    assert proposals[0][0][0] == proposals[1][0][0] == delta.initial
    assert proposals[0][0][1].attempt_id != proposals[1][0][1].attempt_id
    return results


def assert_conflict(delta, contender):
    history = delta.artifacts.lookup_request(delta.binding, contender.request.idempotency_key)
    assert not history.ambiguous
    assert len(history.facts) == 1
    assert history.facts[0].request == contender.request
    assert history.facts[0].status in {c.FactStatus.CONFLICTED, c.FactStatus.QUARANTINED}


def admit_and_trace(delta, contender, reservation):
    row = delta.observer.read(delta.binding.binding_id)  # independent connection read-back
    assert len(delta.observer.rows()) == 1
    assert row.state == c.CoordinationState.RESERVED and row.unresolved
    assert (row.attempt_id, row.holder, row.generation, row.row_version) == (
        reservation.fence.attempt_id, contender.executor.principal_id, 1, 1)
    assert row.active_operation_id == contender.request.operation_id
    with pytest.raises(CoordinationError):
        contender.service.assert_owner(reservation.fence)  # reservation is NOT admission
    claim = contender.service.admit(reservation, delta.preimage, contender.grant)
    assert isinstance(claim, c.AdmissionClaim)
    row = delta.observer.read(delta.binding.binding_id)
    assert row.state == c.CoordinationState.ADMITTED and row.unresolved
    assert (row.attempt_id, row.generation, row.row_version) == (
        claim.attempt_id, claim.generation, claim.row_version)
    assert (row.active_operation_id, row.idempotency_key, row.request_digest) == (
        contender.request.operation_id, contender.request.idempotency_key, contender.request.request_digest)
    assert (row.pre_version_id, row.preimage_digest) == (delta.preimage.version_id, delta.preimage.state_digest)
    assert delta.artifacts.verify_flush(claim)
    send = c.StageEvidence('VC/1.0', c.PatchStage.CONFIG_IN_FLIGHT, Clock().now(), '7' * 64, None)
    contender.service.checkpoint(claim, send.stage, send)
    contender.service.assert_owner(claim)
    # A durable mutation-boundary trace, not a fake Genie PATCH or return-value count.
    contender.artifacts.put('mutation_trace', claim.attempt_id, {
        'attempt_id': claim.attempt_id, 'operation_id': claim.request.operation_id,
        'stage': send.stage.value,
    })
    assert delta.artifacts.get('mutation_trace') == [{
        'attempt_id': claim.attempt_id, 'operation_id': claim.request.operation_id,
        'stage': c.PatchStage.CONFIG_IN_FLIGHT.value,
    }]
    assert delta.observer.read(delta.binding.binding_id).mutation_stage == c.PatchStage.CONFIG_IN_FLIGHT
    assert len(delta.artifacts.get('consumption')) == 1
    return claim


def assert_one_admitted_trace(delta, results):
    winners = [(contender, result) for contender, result in zip(delta.contenders, results)
               if isinstance(result, tuple)]
    assert len(winners) == 1
    assert sum(isinstance(result, OwnershipError) for result in results) == 1
    contender, (reservation, claim) = winners[0]
    assert isinstance(reservation, c.Reservation) and isinstance(claim, c.AdmissionClaim)
    loser = next(item for item in delta.contenders if item is not contender)
    assert_conflict(delta, loser)
    row = delta.observer.read(delta.binding.binding_id)
    assert row.attempt_id == reservation.fence.attempt_id == claim.attempt_id
    assert row.active_operation_id == contender.request.operation_id
    assert len(delta.observer.rows()) == 1
    assert delta.artifacts.get('mutation_trace') == [{
        'attempt_id': claim.attempt_id, 'operation_id': claim.request.operation_id,
        'stage': c.PatchStage.CONFIG_IN_FLIGHT.value,
    }]
    return contender, loser, claim


def test_real_delta_same_row_cas_one_winner(delta):
    assert_one_admitted_trace(delta, race(delta))


@pytest.mark.parametrize('delta', [True], indirect=True, ids=['same-sp'])
def test_real_delta_same_sp_different_attempt_is_fenced(delta):
    winner, loser, claim = assert_one_admitted_trace(delta, race(delta))
    assert winner.executor.principal_id == loser.executor.principal_id
    assert winner.executor.execution_ref != loser.executor.execution_ref
    losing_attempt = loser.store.proposals[0][1].attempt_id
    stale = replace(claim, attempt_id=losing_attempt)
    before = delta.observer.read(delta.binding.binding_id)
    with pytest.raises(OwnershipError):
        loser.service.renew(stale)
    with pytest.raises(OwnershipError):
        loser.service.assert_owner(stale)
    with pytest.raises(OwnershipError):
        loser.service.checkpoint(stale, c.PatchStage.CONFIG_IN_FLIGHT,
            c.StageEvidence('VC/1.0', c.PatchStage.CONFIG_IN_FLIGHT, Clock().now(), '8' * 64, None))
    assert delta.observer.read(delta.binding.binding_id) == before
    assert len(delta.artifacts.get('mutation_trace')) == 1


@pytest.mark.parametrize('delta', [True], indirect=True, ids=['same-sp'])
def test_real_delta_expired_lease_quarantines_without_takeover(delta):
    original = delta.contenders[0]
    reservation = original.service.reserve(delta.binding, original.request, original.executor)
    claim = original.service.admit(reservation, delta.preimage, original.grant)
    delta.expire_lease(claim)
    expired = delta.observer.read(delta.binding.binding_id)
    with pytest.raises(OwnershipError):
        original.service.assert_owner(claim)  # real UPDATE to quarantine, not a renewal
    quarantined = delta.observer.read(delta.binding.binding_id)
    assert quarantined.state == c.CoordinationState.QUARANTINED and quarantined.unresolved
    assert quarantined.quarantine_reason
    assert quarantined.row_version == expired.row_version + 1
    assert (quarantined.attempt_id, quarantined.generation, quarantined.active_operation_id,
            quarantined.pre_version_id, quarantined.lease_expires_at) == (
        claim.attempt_id, claim.generation, claim.request.operation_id,
        delta.preimage.version_id, expired.lease_expires_at)
    history = delta.artifacts.lookup_request(delta.binding, claim.request.idempotency_key)
    assert len(history.facts) == 1 and history.facts[0].status == c.FactStatus.QUARANTINED
    assert history.facts[0].attempt_id == claim.attempt_id
    # Independent same-SP workers issue NEW requests, not duplicate receipts.
    for contender in delta.contenders:
        contender.request = c.RequestIdentity(uid(), uid(), '9' * 64)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda contender: reserve(contender, delta.binding), delta.contenders))
    assert all(isinstance(result, OwnershipError) for result in results)
    for contender in delta.contenders:
        assert_conflict(delta, contender)
        with pytest.raises(OwnershipError):
            contender.service.renew(claim)
        with pytest.raises(OwnershipError):
            contender.service.assert_owner(claim)
    assert delta.observer.read(delta.binding.binding_id) == quarantined
    assert len(delta.observer.rows()) == 1
    assert delta.artifacts.verify_flush(claim)  # durable old claim survives expiry
    assert len(delta.artifacts.get('consumption')) == 1
    assert delta.artifacts.get('mutation_trace') == []


def test_real_delta_seeded_duplicate_rows_block_both_clients(delta):
    delta.seed(delta.initial)  # provisioning owner deliberately violates logical uniqueness
    before = delta.observer.rows()
    assert len(before) == 2
    with pytest.raises(OwnershipError, match='Duplicate'):
        delta.observer.read(delta.binding.binding_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda contender: reserve(contender, delta.binding), delta.contenders))
    assert all(isinstance(result, OwnershipError) for result in results)
    assert all('Duplicate' in str(result) for result in results)
    assert delta.observer.rows() == before  # no UPDATE or INSERT to "repair" authority
    assert all(not contender.store.proposals for contender in delta.contenders)
    assert delta.artifacts.get('consumption') == []
    assert delta.artifacts.get('mutation_trace') == []
