"""Target-local dispatch and durable-request entrypoints; no mutation replay."""

import argparse
from hashlib import sha256
from uuid import UUID

from ..contracts import OperationHandle, OperationStatus
from .provisioning import PROVISION_ROLES

JOB_KINDS = frozenset({"restore", "reconcile", "promotion", "polling", "optimizer_apply",
                       "enrollment", "recovery", "verification", "provision"})


def durable_request(facts, operation_id, workspace_id, kind):
    if str(UUID(operation_id)) != operation_id:
        raise ValueError("Canonical operation_id required")
    request = facts.get_request(operation_id).request
    if request.identity.operation_id != operation_id or request.binding.workspace_id != workspace_id:
        raise PermissionError("Durable operation is not target-local")
    if kind != "verification" and getattr(request, "operation_type", None) != kind:
        raise PermissionError("Job kind does not match durable operation")
    return request


class LocalJobDispatcher:
    def __init__(self, identity, executor, facts, jobs, write_ready):
        self._identity = identity
        self._executor = executor
        self._facts = facts
        self._jobs = dict(jobs)
        self._write_ready = write_ready

    def submit_local(self, operation_id, job_kind):
        if job_kind not in JOB_KINDS or job_kind not in self._jobs:
            raise PermissionError("Job handler not integrated")
        if self._write_ready(job_kind) is not True:
            raise PermissionError("Job safety capabilities or feature switch disabled")
        durable_request(self._facts, operation_id, self._executor.workspace_id, job_kind)
        client = self._identity.verified_client(self._executor)
        job_id = self._jobs[job_kind]
        self._identity.verify_run_as(str(job_id), self._executor.principal_id)
        token = sha256(f"{self._executor.workspace_id}:{job_id}:{job_kind}:{operation_id}".encode()).hexdigest()
        run = client.jobs.run_now(job_id=job_id, job_parameters={"operation_id": operation_id},
                                  idempotency_token=token)
        return OperationHandle(operation_id, OperationStatus.REQUESTED, str(run.run_id))


class GovernedJobRuntime:
    def __init__(self, facts, workspace_id, handlers, verify_only, attempt_state, write_ready):
        self._facts = facts
        self._workspace_id = workspace_id
        self._handlers = dict(handlers)
        self._verify_only = verify_only
        self._attempt_state = attempt_state
        self._write_ready = write_ready

    def run(self, kind, operation_id):
        if kind not in JOB_KINDS or (kind != "verification" and kind not in self._handlers):
            raise PermissionError("Job handler not integrated")
        request = durable_request(self._facts, operation_id, self._workspace_id, kind)
        if kind == "verification":
            return self._verify_only(operation_id)
        state = self._attempt_state(operation_id)
        if state in {"unresolved", "terminal"}:
            return self._verify_only(operation_id)
        if state != "new" or self._write_ready(kind) is not True:
            raise PermissionError("Unknown attempt state or disabled write capability")
        return self._handlers[kind](request)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=sorted(JOB_KINDS))
    parser.add_argument("--operation-id", required=True)
    # Provisioning bootstrap config (populated by the vc-provision DAB task from
    # reviewed bundle variables; ignored by every other kind, which loads a
    # durable request instead). Provisioning precedes the fact tables, so its
    # explicit target config cannot come from a durable request.
    parser.add_argument("--catalog", default=None)
    parser.add_argument("--control-schema", default=None)
    parser.add_argument("--warehouse-id", default=None)
    parser.add_argument("--workspace-id", default=None)
    for role in PROVISION_ROLES:
        parser.add_argument(f"--{role}-principal", default=None)
    args = parser.parse_args(argv)
    if str(UUID(args.operation_id)) != args.operation_id:
        raise ValueError("Canonical operation_id required")
    config = None
    if args.kind == "provision":
        config = {
            "workspace_id": args.workspace_id or "",
            "catalog": args.catalog,
            "control_schema": args.control_schema,
            "warehouse_id": args.warehouse_id,
            "principals": {role: getattr(args, f"{role}_principal") for role in PROVISION_ROLES},
        }
        if not all(config["principals"].values()):
            config["principals"] = {}
    from backend.jobs import build_vc_runtime
    return build_vc_runtime(args.kind, config).run(args.kind, args.operation_id)
