"""Opt-in real-platform probes; never substitute mocks for deployment evidence."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest


class LivePlatform:
    def __init__(self, config):
        self.config = config
        self._verified_roles = set()
        if config.get("disposable_nonproduction") is not True:
            raise ValueError("Explicit disposable nonproduction acknowledgement required")
        for role in config["roles"].values():
            if not all(role.get(key) for key in ("profile", "host", "workspace_id", "principal_id", "warehouse_id")):
                raise ValueError("Explicit role profile, host, workspace, principal and warehouse required")
            if role["profile"].upper() == "DEFAULT":
                raise ValueError("Default profiles are not deployment evidence")

    def _environment(self):
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("DATABRICKS_")}
        if self.config.get("config_file"):
            environment["DATABRICKS_CONFIG_FILE"] = self.config["config_file"]
        return environment

    def _verify_role(self, role):
        if role in self._verified_roles:
            return
        selection = self.config["roles"][role]
        script = """
import json, sys
from databricks.sdk import WorkspaceClient
client = WorkspaceClient(profile=sys.argv[1])
identity = client.api_client.do('GET', '/api/2.0/preview/scim/v2/Me', response_headers=['X-Databricks-Org-Id'])
principal = (identity.get('applicationId') or identity.get('userName')) if client.config.auth_type == 'oauth-m2m' else identity.get('id')
print(json.dumps({'host': client.config.host.rstrip('/'), 'workspace_id': str(identity['X-Databricks-Org-Id']), 'principal_id': principal}))
"""
        result = subprocess.run([sys.executable, "-c", script, selection["profile"]],
                                env=self._environment(), capture_output=True, text=True,
                                timeout=180, check=False)
        if result.returncode:
            raise AssertionError(f"Live identity verification failed for role {role}")
        actual = json.loads(result.stdout)
        for key in ("host", "workspace_id", "principal_id"):
            assert actual[key] == selection[key], f"Explicit {role} {key} mismatch"
        self._verified_roles.add(role)

    def api(self, role, method, path, body=None):
        self._verify_role(role)
        selection = self.config["roles"][role]
        command = ["databricks", "api", method.lower(), path, "--profile", selection["profile"]]
        if body is not None:
            command.extend(["--json", json.dumps(body)])
        result = subprocess.run(command, env=self._environment(), capture_output=True, text=True,
                                timeout=180, check=False)
        if result.returncode:
            raise AssertionError(f"Platform probe failed for role {role}; no permission evidence collected")
        return json.loads(result.stdout)

    def verify_selection(self, selection):
        self._verify_role("provisioner")
        return selection["profile"] == self.config["roles"]["provisioner"]["profile"]

    def command(self, command, *, cwd):
        result = subprocess.run(command, cwd=cwd, env=self._environment(),
                                capture_output=True, text=True, timeout=1800, check=False)
        assert result.returncode == 0, "Live DAB validation/deploy/run failed; not a passing deployment gate"

    def build_artifacts(self, root):
        """Build wheels into the gitignored `.build` dirs before validate/deploy.

        `bundle validate --strict` treats an unmatched `.build` sync glob as a
        warning (fatal in strict mode), so populate it first, exactly as a real
        deploy pipeline does. Commands mirror the artifact `build` hooks in
        databricks.yml, resources/version-control/platform.jobs.yml, and the
        standalone GSO package bundle (packages/genie-space-optimizer)."""
        root = Path(root)
        builds = (
            (root,
             ("rm -f .build/genie_space_optimizer-*.whl && "
              "cd packages/genie-space-optimizer && uv build --wheel --out-dir ../../.build && "
              "cd ../.. && cp .build/genie_space_optimizer-*.whl "
              ".build/genie_space_optimizer-0.0.0-py3-none-any.whl")),
            (root, "uv build --wheel --out-dir .build"),
            (root / "packages/genie-space-optimizer",
             ("rm -f .build/genie_space_optimizer-*.whl && uv build --wheel --out-dir .build && "
              "cp .build/genie_space_optimizer-*.whl "
              ".build/genie_space_optimizer-0.0.0-py3-none-any.whl")),
        )
        for cwd, script in builds:
            result = subprocess.run(["sh", "-c", script], cwd=cwd, env=self._environment(),
                                    capture_output=True, text=True, timeout=1800, check=False)
            assert result.returncode == 0, f"Artifact build failed ({cwd}): {result.stderr}"

    def content_fingerprint(self):
        spaces = self.config["governed_spaces"]
        assert spaces, "Explicit governed-space inventory required"
        contents = {}
        for space_id in spaces:
            space = self.api("provisioner", "get", f"/api/2.0/genie/spaces/{space_id}?include_serialized_space=true")
            assert "serialized_space" in space, "Full config read required"
            contents[space_id] = {key: space.get(key) for key in ("serialized_space", "description", "title")}
        return sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _durable_table_properties(properties):
        """Keep only the properties provisioning owns as the durable contract.

        The UC table `properties` map also carries per-commit and statistics
        metadata (delta.lastCommitTimestamp, delta.lastUpdateVersion, row-tracking
        column UUIDs, spark.sql.statistics.*, the redundant tableId). Those change
        on every commit — including the idempotent ALTER DROP/ADD CONSTRAINT the
        provisioner re-issues — so a repeatable-provisioning gate must compare the
        end-state (appendOnly, CHECK constraints, protocol version, features), not
        the commit history."""
        volatile_prefixes = ("spark.sql.statistics.", "delta.rowTracking.materialized")
        volatile_keys = {"delta.lastCommitTimestamp", "delta.lastUpdateVersion", "io.unitycatalog.tableId"}
        return {key: value for key, value in (properties or {}).items()
                if key not in volatile_keys and not key.startswith(volatile_prefixes)}

    def provisioning_fingerprint(self):
        from backend.services.version_control.platform.provisioning import OWNER_SPECS
        resources = {}
        for owner, kind, name in OWNER_SPECS:
            if kind == "table":
                resource = self.table(name)
                resources[name] = {key: resource.get(key) for key in ("table_id", "owner", "columns")}
                resources[name]["properties"] = self._durable_table_properties(resource.get("properties"))
            else:
                resource = self.api("provisioner", "get", f"/api/2.1/unity-catalog/volumes/{self.config['namespace']}.{name}")
                resources[name] = {key: resource.get(key) for key in ("volume_id", "owner", "full_name", "volume_type")}
        return sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()

    def principal(self, role):
        return self.config["roles"][role]["principal_id"]

    def table(self, name):
        return self.api("provisioner", "get", f"/api/2.1/unity-catalog/tables/{self.config['namespace']}.{name}")

    def exec_sql(self, role, statement):
        """Run a raw SQL statement as `role` and return the full API response.

        Most probes are declared in owner_probes and run via sql(); exec_sql is
        for the tamper-evidence scenario, which provisions a disposable scratch
        table with a per-run (uuid) name that cannot be pre-declared."""
        self._verify_role(role)
        response = self.api(role, "post", "/api/2.0/sql/statements", {
            "warehouse_id": self.config["roles"][role]["warehouse_id"],
            "statement": statement, "wait_timeout": "10s", "on_wait_timeout": "CONTINUE",
        })
        deadline = time.monotonic() + 180
        while response["status"]["state"] in {"PENDING", "RUNNING"}:
            if time.monotonic() >= deadline:
                raise AssertionError("Platform SQL probe timed out, not a denial proof")
            time.sleep(2)
            response = self.api(role, "get", f"/api/2.0/sql/statements/{response['statement_id']}")
        return response

    def sql(self, role, probe):
        return self.exec_sql(role, self.config["owner_probes"][probe])

    @staticmethod
    def _rows(response):
        columns = [column["name"] for column in response["manifest"]["schema"]["columns"]]
        return [dict(zip(columns, row))
                for row in (response.get("result", {}) or {}).get("data_array", []) or []]

    def describe_history(self, name, limit=20):
        """Delta transaction history is the per-table, synchronous audit log: every
        commit (WRITE, ALTER, SET TBLPROPERTIES, REPLACE) is recorded with the
        acting principal. This is the tamper-evidence substrate for the reframed
        append-only guarantee (UC MODIFY also confers ALTER/REPLACE, so writer
        immutability is by durable detection, not by grant)."""
        response = self.exec_sql("provisioner",
                                 f"DESCRIBE HISTORY {self.config['namespace']}.{name} LIMIT {limit}")
        assert response["status"]["state"] == "SUCCEEDED", "Delta history must be readable"
        return self._rows(response)

    def provision_scratch_appendonly_table(self, writer):
        """Owner creates a disposable append-only table and grants `writer` the same
        SELECT+MODIFY the real runtime holds. Returns its (unqualified) name."""
        from uuid import uuid4
        name = f"vc_tamper_probe_{uuid4().hex}"
        fqn = f"{self.config['namespace']}.{name}"
        grantee = "`" + self.principal(writer).replace("`", "``") + "`"
        for statement in (
            (f"CREATE TABLE {fqn} (x STRING) USING DELTA "
             "TBLPROPERTIES ('delta.appendOnly' = 'true')"),
            f"GRANT SELECT, MODIFY ON TABLE {fqn} TO {grantee}",
        ):
            assert self.exec_sql("provisioner", statement)["status"]["state"] == "SUCCEEDED"
        return name

    def drop_owner_table(self, name):
        self.exec_sql("provisioner", f"DROP TABLE IF EXISTS {self.config['namespace']}.{name}")

    def assert_sql_succeeds(self, role, probe):
        assert self.sql(role, probe)["status"]["state"] == "SUCCEEDED"

    def assert_sql_denied(self, role, probe):
        status = self.sql(role, probe)["status"]
        assert status["state"] == "FAILED"
        error = status.get("error", {})
        # The SQL Statements API surfaces UC permission failures as
        # error_code=BAD_REQUEST with the real reason in the message, so match on
        # either the structured code or the PERMISSION_DENIED/INSUFFICIENT text.
        message = error.get("message", "")
        assert (error.get("error_code") in {"PERMISSION_DENIED", "INSUFFICIENT_PERMISSIONS"}
                or "PERMISSION_DENIED" in message
                or "INSUFFICIENT_PERMISSIONS" in message
                or "does not have" in message)

    def _volume_file(self, volume, role):
        catalog, schema = self.config["namespace"].split(".")
        return f"/Volumes/{catalog}/{schema}/{volume}/write_probe_{role}.txt"

    def _files_api(self, role, action, volume):
        """Exercise a UC Volume via the SDK Files API as `role`. WRITE/READ VOLUME
        are file-level privileges with no SQL DML, and `databricks fs` swallows
        denials into an empty error, so we drive files.upload/list directly (in a
        profile-isolated subprocess) to get the structured PermissionDenied.
        Returns (ok, message)."""
        self._verify_role(role)
        profile = self.config["roles"][role]["profile"]
        path = self._volume_file(volume, role)
        script = """
import io, sys
from databricks.sdk import WorkspaceClient
client = WorkspaceClient(profile=sys.argv[1])
action, path = sys.argv[2], sys.argv[3]
try:
    if action == 'write':
        client.files.upload(path, io.BytesIO(b'vc-write-probe'), overwrite=True)
    else:
        list(client.files.list_directory_contents(path.rsplit('/', 1)[0] + '/'))
    print('OK')
except Exception as exc:
    print('ERR ' + type(exc).__name__ + ': ' + str(exc))
"""
        result = subprocess.run([sys.executable, "-c", script, profile, action, path],
                                env=self._environment(), capture_output=True, text=True,
                                timeout=180, check=False)
        output = (result.stdout + result.stderr).strip()
        return output.startswith("OK"), output

    def assert_volume_write_succeeds(self, role, volume):
        ok, message = self._files_api(role, "write", volume)
        assert ok, f"Expected {role} to write {volume}; got: {message[:200]}"

    def assert_volume_write_denied(self, role, volume):
        ok, message = self._files_api(role, "write", volume)
        assert not ok, f"Expected {role} to be denied write on {volume}"
        assert ("PermissionDenied" in message or "PERMISSION_DENIED" in message
                or "does not have" in message or "not authorized" in message.lower())

    def assert_volume_read_succeeds(self, role, volume):
        ok, message = self._files_api(role, "read", volume)
        assert ok, f"Expected {role} to read {volume}; got: {message[:200]}"

    def assert_sql_appendonly_rejected(self, role, probe):
        """UPDATE/DELETE on a fact table is rejected by delta.appendOnly, not by
        a permission (UC has no INSERT/UPDATE privilege; the runtime holds MODIFY
        bounded by the append-only table property). This is the append-only proof."""
        status = self.sql(role, probe)["status"]
        assert status["state"] == "FAILED"
        error = status.get("error", {})
        message = error.get("message", "")
        assert ("APPEND_ONLY" in error.get("error_code", "")
                or "DELTA_CANNOT_MODIFY_APPEND_ONLY" in message
                or "append only" in message.lower())


@pytest.fixture
def live_platform():
    location = os.environ.get("VC_INTEGRATION_CONFIG")
    if not location:
        pytest.fail("DEPLOYMENT BLOCKER: set VC_INTEGRATION_CONFIG for real nonproduction probes")
    return LivePlatform(json.loads(Path(location).read_text()))


def _statement_parameters(parameters):
    """Render a named-parameter dict into the SQL Statements API `parameters`
    list with explicit types, so INSERTs land in the typed operations columns.

    DeltaFactStore hands to_wire() values: strings, ints (BIGINT columns),
    booleans, one ISO-8601 TIMESTAMP (`recorded_at`) and typed NULLs. The direct
    SQL connector would bind these natively, but the workspace IP ACL blocks the
    thrift warehouse endpoint, so we drive the REST Statements API instead."""
    from datetime import datetime
    rendered = []
    for name, value in (parameters or {}).items():
        if value is None:
            rendered.append({"name": name, "value": None})  # typed NULL (VOID)
        elif isinstance(value, bool):
            rendered.append({"name": name, "value": str(value).lower(), "type": "BOOLEAN"})
        elif isinstance(value, int):
            rendered.append({"name": name, "value": str(value), "type": "BIGINT"})
        elif name.endswith("_at"):
            moment = datetime.fromisoformat(str(value))
            rendered.append({"name": name,
                             "value": moment.strftime("%Y-%m-%d %H:%M:%S.%f"),
                             "type": "TIMESTAMP"})
        else:
            rendered.append({"name": name, "value": str(value), "type": "STRING"})
    return rendered


class M06Platform:
    """Real M08 platform surface for the audit/authorization deployment gate.

    Builds the target-owned durable fact store (DeltaFactStore + M06
    DurableOperationFacts) over live Databricks SQL warehouses authenticated as
    the explicit role service principals. No mocks: every probe is a real commit,
    read-back, or denial against the provisioned namespace. SQL flows through the
    REST Statements API (the thrift warehouse endpoint is IP-ACL blocked).
    """

    def __init__(self, config):
        self.config = config
        namespace = config["namespace"]
        self.catalog, self.control_schema = namespace.split(".")
        self.operations_table = f"`{self.catalog}`.`{self.control_schema}`.`genie_space_operations`"
        self.target_workspace_id = config["roles"]["executor"]["workspace_id"]
        self._live = LivePlatform(config)

    # -- live SQL (REST Statements API) -----------------------------------
    def _execute(self, role, statement, parameters=None):
        import time
        self._live._verify_role(role)
        body = {"warehouse_id": self.config["roles"][role]["warehouse_id"],
                "statement": statement, "wait_timeout": "30s", "on_wait_timeout": "CONTINUE"}
        rendered = _statement_parameters(parameters)
        if rendered:
            body["parameters"] = rendered
        response = self._live.api(role, "post", "/api/2.0/sql/statements", body)
        deadline = time.monotonic() + 180
        while response["status"]["state"] in {"PENDING", "RUNNING"}:
            if time.monotonic() >= deadline:
                raise RuntimeError("SQL statement timed out")
            time.sleep(2)
            response = self._live.api(role, "get", f"/api/2.0/sql/statements/{response['statement_id']}")
        if response["status"]["state"] != "SUCCEEDED":
            error = response["status"].get("error", {})
            raise RuntimeError(f"{error.get('error_code', 'SQL_FAILED')}: {error.get('message', '')}")
        return LivePlatform._rows(response)

    def sql(self, role):
        return lambda statement, parameters=None: self._execute(role, statement, parameters)

    def _denying(self, role):
        """Return a (statement, parameters) runner that converts any server-side
        rejection (permission or delta.appendOnly) into PermissionError, so the
        gate can assert denial uniformly."""
        def run(statement, parameters=None):
            try:
                return self._execute(role, statement, parameters)
            except Exception as exc:
                raise PermissionError(str(exc)[:400]) from exc
        return run

    def target_sql(self, statement, parameters=None):
        return self._denying("executor")(statement, parameters)

    def source_sql(self, statement, parameters=None):
        return self._denying("source")(statement, parameters)

    # -- durable fact store (M06) -----------------------------------------
    def _build_ledger(self):
        """Construct a live target-owned DurableOperationFacts over DeltaFactStore
        and return (ledger, store) so callers can retain the store handle (the
        outage probes patch its SQL and private-evidence readers)."""
        from backend.services.version_control.governance.delta import DeltaFactStore
        from backend.services.version_control.governance.facts import (
            DurableOperationFacts,
        )

        store = DeltaFactStore(self.sql("executor"), self.catalog, self.control_schema,
                               self.target_workspace_id, writes_enabled=True,
                               read_evidence=self._read_evidence)
        return DurableOperationFacts(store.read, store.append, writes_enabled=True), store

    def operation_facts(self):
        return self._build_ledger()[0]

    def _read_evidence(self, uri):
        # Private preflight evidence lives in the approval-owned Volume; it is read
        # back under the approval role (the Volume's writer/reader), not the
        # executor, which holds no grant on vc_approval_evidence.
        selection = self.config["roles"]["approval"]
        script = (
            "import sys\n"
            "from databricks.sdk import WorkspaceClient\n"
            "client = WorkspaceClient(profile=sys.argv[1])\n"
            "sys.stdout.buffer.write(client.files.download(sys.argv[2]).contents.read())\n")
        result = subprocess.run([sys.executable, "-c", script, selection["profile"], uri],
                                env=self._live._environment(), capture_output=True, timeout=180, check=False)
        if result.returncode:
            raise PermissionError("Private evidence unreadable")
        return result.stdout

    def valid_fact(self):
        from dataclasses import replace
        from datetime import UTC, datetime
        from uuid import NAMESPACE_URL, uuid4, uuid5

        from backend.services.version_control.contracts import (
            ActorContext,
            BindingRef,
            FactKind,
            FactStatus,
            OperationFact,
            PatchStage,
            RequestIdentity,
            StageEvidence,
        )
        from backend.services.version_control.governance.facts import fact_key

        ws = self.target_workspace_id
        digest = "a" * 64
        operation_id = str(uuid4())
        now = datetime.now(UTC)
        binding = BindingRef(str(uuid4()), 1, "audit-gate", ws, "physical-space", "dev")
        actor = ActorContext("audit-actor@example.invalid", ws, "human")
        provisional = OperationFact(
            event_id=str(uuid4()), fact_kind=FactKind.OPERATION, operation_id=operation_id,
            transition_sequence=0, event_key="", binding=binding,
            request=RequestIdentity(operation_id, str(uuid4()), digest), operation_type="edit",
            requester_id=actor.subject_id, actor=actor, status=FactStatus.REQUESTED,
            evidence=StageEvidence("VC/1.0", PatchStage.CONFIG_PENDING, now, digest, None),
            recorded_at=now)
        key = fact_key(provisional)
        return replace(provisional, event_key=key, event_id=str(uuid5(NAMESPACE_URL, key)))

    # -- untrusted-source isolation (test #15) ----------------------------
    def _capture_insert(self, fact):
        """Render the exact INSERT `DeltaFactStore.append` would emit for `fact`
        without executing it, so the gate can replay it as the target executor
        (allowed) and as the untrusted source (must be denied)."""
        from backend.services.version_control.contracts import to_wire
        from backend.services.version_control.governance.delta import DeltaFactStore

        captured = {}

        def recorder(statement, parameters=None):
            captured["statement"] = statement
            captured["parameters"] = parameters
            return []

        store = DeltaFactStore(recorder, self.catalog, self.control_schema,
                               self.target_workspace_id, writes_enabled=True)
        store.append(fact.event_key, to_wire(fact))
        return captured["statement"], captured["parameters"]

    def valid_insert(self, kind):
        """Build a target-valid ``approval_request`` or ``receipt`` fact and return
        the (statement, parameters) that appends it. Only the target-owned runtime
        holds SELECT+MODIFY on the operations table, so a source replay of the
        identical, schema-valid INSERT must be denied by UC — the fact boundary is
        the grant, not the JSON ``fact_kind`` or any workspace predicate."""
        from dataclasses import replace
        from datetime import UTC, datetime, timedelta
        from uuid import NAMESPACE_URL, uuid4, uuid5

        from backend.services.version_control.contracts import (
            ActorContext,
            ApprovalInputs,
            BindingRef,
            DeploymentReceipt,
            FactKind,
            FactStatus,
            Fingerprints,
            OperationFact,
            OperationStatus,
            RequestIdentity,
        )
        from backend.services.version_control.governance.facts import fact_key

        ws = self.target_workspace_id
        d = "a" * 64
        operation_id = str(uuid4())
        now = datetime.now(UTC)
        binding = BindingRef(str(uuid4()), 1, "audit-gate", ws, "physical-space", "dev")
        actor = ActorContext("audit-actor@example.invalid", ws, "human")
        fp = Fingerprints(d, d, d, "vc-c14n/1")
        if kind == "approval_request":
            fact_kind = FactKind.APPROVAL_REQUEST
            evidence = ApprovalInputs(
                "VC/1.0", operation_id, "promotion", str(uuid4()), d, fp, None, None,
                d, None, "vc-c14n/1", binding, fp, None, d, d, d, {"minimum": 1},
                actor.subject_id, {"verify_only": True}, now + timedelta(hours=1))
        elif kind == "receipt":
            fact_kind = FactKind.RECEIPT
            evidence = DeploymentReceipt(
                schema_version="VC/1.0", release_id=str(uuid4()), operation_id=operation_id,
                target_binding=binding, attempt_id=str(uuid4()), generation=1,
                approval_id=str(uuid4()), approval_digest=d, source_version_id=str(uuid4()),
                package_digest=d, mapping_digest=d, intended_fingerprints=fp,
                rendered_fingerprints=fp, observed_fingerprints=fp, pre_version_id=str(uuid4()),
                post_version_id=str(uuid4()), transformer_version="vc-x/1",
                canonicalizer_version="vc-c14n/1", executor_id=actor.subject_id,
                job_run_id="run-1", validation_evidence_digest=d, benchmark_evidence_digest=d,
                status=OperationStatus.CONFIRMED, compensation_operation_id=None, recorded_at=now)
        else:
            raise ValueError(f"Unknown valid_insert kind: {kind!r}")
        provisional = OperationFact(
            event_id=str(uuid4()), fact_kind=fact_kind, operation_id=operation_id,
            transition_sequence=0, event_key="", binding=binding,
            request=RequestIdentity(operation_id, str(uuid4()), d),
            operation_type="promotion", requester_id=actor.subject_id, actor=actor,
            status=FactStatus.REQUESTED, evidence=evidence, recorded_at=now)
        key = fact_key(provisional)
        finished = replace(provisional, event_key=key,
                           event_id=str(uuid5(NAMESPACE_URL, key)))
        return self._capture_insert(finished)

    def read_approved_export_as_source(self):
        """The source SP is granted READ VOLUME on the target-published receipts
        export (`vc_target_receipts`), a same-metastore shared-grant surface, so
        listing it must succeed."""
        ok, message = self._live._files_api("source", "read", "vc_target_receipts")
        if not ok:
            raise AssertionError(
                f"source should read the approved export volume; got: {message[:200]}")
        return True

    def read_private_evidence_as_source(self):
        """The source SP holds no grant on the private approval-evidence Volume
        (`vc_approval_evidence`), so a read must raise PermissionError."""
        ok, message = self._live._files_api("source", "read", "vc_approval_evidence")
        if ok:
            raise AssertionError("source unexpectedly read the private evidence volume")
        raise PermissionError(message[:400])

    # -- live identity provider (M08) -------------------------------------
    def _sdk_client(self, profile):
        """Build an in-process WorkspaceClient bound to `profile`, ignoring any
        ambient DATABRICKS_* env so the profile (and optional config_file) is the
        sole credential — the identity provider must call jobs.get / SCIM in
        process (not via the CLI subprocess helpers)."""
        from databricks.sdk import WorkspaceClient

        saved = {name: os.environ.pop(name) for name in list(os.environ)
                 if name.startswith("DATABRICKS_")}
        try:
            kwargs = {"profile": profile}
            if self.config.get("config_file"):
                kwargs["config_file"] = self.config["config_file"]
            return WorkspaceClient(**kwargs)
        finally:
            os.environ.update(saved)

    @property
    def _directory(self):
        """Target-local directory reader (admin) for live SCIM group/actor
        resolution; service principals cannot read arbitrary Users/Groups."""
        if getattr(self, "_directory_client", None) is None:
            profile = self.config["approval_identities"]["directory_profile"]
            self._directory_client = self._sdk_client(profile)
        return self._directory_client

    @property
    def identity(self):
        if getattr(self, "_identity_provider", None) is None:
            from backend.services.version_control.platform.identity import (
                PlatformIdentityProvider,
            )

            profile = self.config["roles"]["executor"]["profile"]
            self._identity_provider = PlatformIdentityProvider(
                profiles={profile: lambda profile=profile: self._sdk_client(profile)},
                request_resolver=self._resolve_actor,
                group_resolver=self._resolve_groups,
                edit_resolver=None,
                job_client=self._sdk_client(profile))
        return self._identity_provider

    def _resolve_groups(self, subject_id, workspace_id):
        client = self._directory
        try:
            user = client.users.get(subject_id)
            return frozenset(group.display for group in (user.groups or []))
        except Exception:  # noqa: BLE001 - SCIM user miss -> fall back to SP lookup
            for sp in client.service_principals.list(
                    filter=f'applicationId eq "{subject_id}"'):
                return frozenset(group.display for group in (sp.groups or []))
        raise PermissionError(f"Unknown subject for group resolution: {subject_id}")

    def _resolve_actor(self, authentication_reference):
        from backend.services.version_control.contracts import ActorContext

        client = self._directory
        try:
            client.users.get(authentication_reference)
            kind = "human"
        except Exception:  # noqa: BLE001 - SCIM user miss -> classify as service principal
            if not list(client.service_principals.list(
                    filter=f'applicationId eq "{authentication_reference}"')):
                raise PermissionError(f"Unknown authenticated subject: {authentication_reference}")
            kind = "service"
        return ActorContext(authentication_reference, self.target_workspace_id, kind)

    def authenticated_human(self):
        from backend.services.version_control.contracts import AuthenticatedRequest

        reference = self.config["approval_identities"]["approvers"][0]["id"]
        return self.identity.actor(AuthenticatedRequest(reference, self.target_workspace_id))

    def authenticated_service(self):
        from backend.services.version_control.contracts import AuthenticatedRequest

        reference = self.config["roles"]["executor"]["principal_id"]
        return self.identity.actor(AuthenticatedRequest(reference, self.target_workspace_id))

    # -- production approval orchestration (tests #7/#12 live) -------------
    def _upload_private_evidence(self, operation_id):
        """Write the large preflight evidence blob to the approval-owned Volume as
        the approval role and return (uri, sha256); the fact store verifies the
        digest on append and re-reads it during authorization."""
        blob = b"vc-preflight-evidence:" + operation_id.encode()
        uri = (f"/Volumes/{self.catalog}/{self.control_schema}/vc_approval_evidence/"
               f"{operation_id}/preflight.bin")
        profile = self.config["roles"]["approval"]["profile"]
        script = (
            "import io, sys\n"
            "from databricks.sdk import WorkspaceClient\n"
            "client = WorkspaceClient(profile=sys.argv[1])\n"
            "client.files.upload(sys.argv[2], io.BytesIO(sys.stdin.buffer.read()), overwrite=True)\n")
        result = subprocess.run([sys.executable, "-c", script, profile, uri], input=blob,
                                env=self._live._environment(), capture_output=True, timeout=180, check=False)
        if result.returncode:
            raise RuntimeError(f"Private evidence upload failed: {result.stderr.decode()[:200]}")
        return uri, sha256(blob).hexdigest()

    def approved_production_request(self):
        """Drive a real production approval to a grantable state over live Delta
        facts + SCIM identity: enrol nothing new, record the reviewed request with
        durable preflight evidence, request approval as the requester SP, and cast
        two distinct human approve votes. Returns (service, request, executor,
        human) so the gate can assert authorize() succeeds and that revoking a
        human's approver membership or an evidence/Delta outage blocks it."""
        from dataclasses import replace
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from uuid import NAMESPACE_URL, uuid4, uuid5

        from backend.services.version_control.contracts import (
            ActorContext,
            ApprovalInputs,
            AuthenticatedRequest,
            BindingRef,
            ExplicitExecutorSelection,
            FactKind,
            FactStatus,
            Fingerprints,
            MutationRequest,
            OperationFact,
            PatchStage,
            RequestIdentity,
            StageEvidence,
            canonical_json_hash,
        )
        from backend.services.version_control.governance.approvals import (
            ApprovalService,
            request_digest,
        )
        from backend.services.version_control.governance.facts import fact_key

        ws = self.target_workspace_id
        ident = self.config["approval_identities"]
        requester_id = ident["requester_principal_id"]
        executor_role = self.config["roles"]["executor"]
        now = datetime.now(UTC)
        digest = "a" * 64
        operation_id = str(uuid4())
        binding = BindingRef(str(uuid4()), 1, "audit-approval", ws, "physical-space", "prod")
        fp = Fingerprints(digest, digest, digest, "vc-c14n/1")

        # Reviewed request bound to immutable inputs (mirrors ApprovalService rules).
        request = MutationRequest(
            RequestIdentity(operation_id, str(uuid4()), digest), binding, "edit",
            str(uuid4()), fp.state_digest, {"instructions": []}, None, operation_id)
        rendered = canonical_json_hash("vc-rendered-target/1", {
            "serialized_space": request.serialized_space, "description": request.description})
        inputs = ApprovalInputs(
            "VC/1.0", operation_id, "edit", request.source_version_id, digest, fp, None, None,
            digest, None, "vc-c14n/1", binding, fp, None, digest, digest, digest,
            {"minimum": 1}, requester_id, {"verify_only": True}, now + timedelta(hours=1))
        inputs = replace(inputs, rendered_target_digest=rendered)
        request = replace(request, identity=replace(request.identity,
                                                     request_digest=request_digest(request)))

        # Durable preflight evidence: inline digest for _bound, plus the large blob
        # in the approval Volume so the evidence-outage probe has a real reader.
        evidence_uri, evidence_sha = self._upload_private_evidence(operation_id)
        provisional = OperationFact(
            event_id=str(uuid4()), fact_kind=FactKind.OPERATION, operation_id=operation_id,
            transition_sequence=0, event_key="", binding=binding, request=request.identity,
            operation_type="edit", requester_id=requester_id,
            actor=ActorContext(requester_id, ws, "service"), status=FactStatus.PREIMAGE_CAPTURED,
            evidence=StageEvidence("VC/1.0", PatchStage.CONFIG_PENDING, now, digest, None),
            recorded_at=now, evidence_uri=evidence_uri, evidence_digest=evidence_sha)
        key = fact_key(provisional)
        op_fact = replace(provisional, event_key=key, event_id=str(uuid5(NAMESPACE_URL, key)))

        ledger, store = self._build_ledger()
        ledger.record_request(request, op_fact)
        registry = SimpleNamespace(resolve=lambda binding_id: binding)
        service = ApprovalService(ledger, self.identity, registry, lambda: now,
                                  target_identity=lambda selection: self.identity,
                                  writes_enabled=True)

        requester_actor = self.identity.actor(AuthenticatedRequest(requester_id, ws))
        approval = service.request(inputs, requester_actor)
        executor = self.identity.executor(ExplicitExecutorSelection(
            ws, executor_role["host"], executor_role["principal_id"],
            ident["execution_ref"], executor_role["profile"]))
        approvers = [self.identity.actor(AuthenticatedRequest(entry["id"], ws))
                     for entry in ident["approvers"]]
        for approver in approvers:
            service.vote(approval.approval_id, "approve", approver)

        self._last_ledger, self._last_store = ledger, store
        return service, request, executor, approvers[0]

    def revoke_target_approver_membership(self, human):
        """Context manager that removes `human` from the approver policy groups so
        the target-side re-check at authorization fails, then restores them."""
        return self._membership_suspended(human.subject_id)

    @contextmanager
    def _membership_suspended(self, subject_id):
        from databricks.sdk.service.iam import (
            ComplexValue,
            Patch,
            PatchOp,
            PatchSchema,
        )

        groups = self.config["approval_identities"]["groups"]
        client = self._directory
        targets = [groups["approvers"], groups["target-approvers"]]
        schema = [PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP]
        for group_id in targets:
            client.groups.patch(group_id, operations=[Patch(
                op=PatchOp.REMOVE, path=f'members[value eq "{subject_id}"]')], schemas=schema)
        self._await_membership(subject_id, present=False)
        try:
            yield
        finally:
            for group_id in targets:
                client.groups.patch(group_id, operations=[Patch(
                    op=PatchOp.ADD, path="members",
                    value=[ComplexValue(value=subject_id).as_dict()])], schemas=schema)
            self._await_membership(subject_id, present=True)

    def _await_membership(self, subject_id, *, present, timeout=30):
        """Block until the directory reflects the membership change, so the gate's
        authorize() observes the revocation (or restoration) rather than racing a
        SCIM read replica."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            has = "approvers" in self._resolve_groups(subject_id, self.target_workspace_id)
            if has == present:
                return
            time.sleep(2)
        raise AssertionError(
            f"Directory did not reflect approvers membership present={present} for {subject_id}")

    @contextmanager
    def deny_operation_reads(self):
        """Simulate a Delta fact-store outage: committed reads raise, so no earlier
        positive authorization can be reused (FM-OUTAGE)."""
        original = self._last_ledger._read

        def outage():
            raise OSError("operation fact store unavailable")

        self._last_ledger._read = outage
        try:
            yield
        finally:
            self._last_ledger._read = original

    @contextmanager
    def deny_private_evidence_reads(self):
        """Simulate a private-evidence Volume outage: the fact store can no longer
        verify the durable preflight blob, so authorization fails closed."""
        original = self._last_store.read_evidence

        def outage(_uri):
            raise OSError("private evidence volume unavailable")

        self._last_store.read_evidence = outage
        try:
            yield
        finally:
            self._last_store.read_evidence = original

    def count_event(self, event_key):
        rows = self._execute("executor",
                             f"SELECT COUNT(*) AS n FROM {self.operations_table} WHERE event_key = :key",
                             {"key": event_key})
        return int(rows[0]["n"])

    def close(self):
        # REST Statements API is stateless; nothing to tear down.
        pass


@pytest.fixture
def m06_platform():
    location = os.environ.get("VC_INTEGRATION_CONFIG")
    if not location:
        pytest.fail("DEPLOYMENT BLOCKER: set VC_INTEGRATION_CONFIG for the M06/M08 audit gate")
    platform = M06Platform(json.loads(Path(location).read_text()))
    try:
        yield platform
    finally:
        platform.close()
