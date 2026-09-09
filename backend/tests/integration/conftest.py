"""Opt-in real-platform probes; never substitute mocks for deployment evidence."""

import json
import os
import subprocess
import sys
import time
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
    def operation_facts(self):
        from backend.services.version_control.governance.delta import DeltaFactStore
        from backend.services.version_control.governance.facts import (
            DurableOperationFacts,
        )

        store = DeltaFactStore(self.sql("executor"), self.catalog, self.control_schema,
                               self.target_workspace_id, writes_enabled=True,
                               read_evidence=self._read_evidence)
        return DurableOperationFacts(store.read, store.append, writes_enabled=True)

    def _read_evidence(self, uri):
        selection = self.config["roles"]["executor"]
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
