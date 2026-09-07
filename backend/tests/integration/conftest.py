"""Opt-in real-platform probes; never substitute mocks for deployment evidence."""

import json
import os
from pathlib import Path
import subprocess
import time

import pytest


class LivePlatform:
    def __init__(self, config):
        self.config = config
        if config.get("disposable_nonproduction") is not True:
            raise ValueError("Explicit disposable nonproduction acknowledgement required")
        for role in config["roles"].values():
            if not all(role.get(key) for key in ("profile", "host", "workspace_id", "principal_id", "warehouse_id")):
                raise ValueError("Explicit role profile, host, workspace, principal and warehouse required")
            if role["profile"].upper() == "DEFAULT":
                raise ValueError("Default profiles are not deployment evidence")

    def api(self, role, method, path, body=None):
        selection = self.config["roles"][role]
        command = ["databricks", "api", method.lower(), path, "--profile", selection["profile"]]
        if body is not None:
            command.extend(["--json", json.dumps(body)])
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("DATABRICKS_")}
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise AssertionError(f"Platform probe failed for role {role}; no permission evidence collected")
        return json.loads(result.stdout)

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


@pytest.fixture
def live_platform():
    location = os.environ.get("VC_INTEGRATION_CONFIG")
    if not location:
        pytest.fail("DEPLOYMENT BLOCKER: set VC_INTEGRATION_CONFIG for real nonproduction probes")
    return LivePlatform(json.loads(Path(location).read_text()))
