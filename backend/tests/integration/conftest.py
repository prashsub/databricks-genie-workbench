"""Opt-in real-platform probes; never substitute mocks for deployment evidence."""

import json
import os
from pathlib import Path
import subprocess
import time
import sys
from hashlib import sha256

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
                                env=self._environment(), capture_output=True, text=True, timeout=180)
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
        result = subprocess.run(command, env=self._environment(), capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise AssertionError(f"Platform probe failed for role {role}; no permission evidence collected")
        return json.loads(result.stdout)

    def verify_selection(self, selection):
        self._verify_role("provisioner")
        return selection["profile"] == self.config["roles"]["provisioner"]["profile"]

    def command(self, command, *, cwd):
        result = subprocess.run(command, cwd=cwd, env=self._environment(),
                                capture_output=True, text=True, timeout=1800)
        assert result.returncode == 0, "Live DAB validation/deploy/run failed; not a passing deployment gate"

    def content_fingerprint(self):
        spaces = self.config["governed_spaces"]
        assert spaces, "Explicit governed-space inventory required"
        contents = {}
        for space_id in spaces:
            space = self.api("provisioner", "get", f"/api/2.0/genie/spaces/{space_id}?include_serialized_space=true")
            assert "serialized_space" in space, "Full config read required"
            contents[space_id] = {key: space.get(key) for key in ("serialized_space", "description", "title")}
        return sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()

    def provisioning_fingerprint(self):
        from backend.services.version_control.platform.provisioning import OWNER_SPECS
        resources = {}
        for owner, kind, name in OWNER_SPECS:
            if kind == "table":
                resource = self.table(name)
                resources[name] = {key: resource.get(key) for key in ("table_id", "owner", "columns", "properties")}
            else:
                resource = self.api("provisioner", "get", f"/api/2.1/unity-catalog/volumes/{self.config['namespace']}.{name}")
                resources[name] = {key: resource.get(key) for key in ("volume_id", "owner", "full_name", "volume_type")}
        return sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()

    def principal(self, role):
        return self.config["roles"][role]["principal_id"]

    def table(self, name):
        return self.api("provisioner", "get", f"/api/2.1/unity-catalog/tables/{self.config['namespace']}.{name}")

    def sql(self, role, probe):
        statement = self.config["owner_probes"][probe]
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

    def assert_sql_succeeds(self, role, probe):
        assert self.sql(role, probe)["status"]["state"] == "SUCCEEDED"

    def assert_sql_denied(self, role, probe):
        status = self.sql(role, probe)["status"]
        assert status["state"] == "FAILED"
        error = status.get("error", {})
        assert (error.get("error_code") in {"PERMISSION_DENIED", "INSUFFICIENT_PERMISSIONS"}
                or "INSUFFICIENT_PERMISSIONS" in error.get("message", ""))

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
