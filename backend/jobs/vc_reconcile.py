"""Target-local scheduled observation; no mutation implementation or fallback."""

import argparse
from uuid import UUID

from backend.services.version_control import contracts as vc


def run_scan(workspace_id, cursor, *, service, executor):
    if (executor.workspace_id != workspace_id or executor.actor_kind != "service"
            or not executor.execution_ref.startswith("job/") or service.executor != executor):
        raise PermissionError("Reconciliation scan requires its target-local Job executor")
    if service.identity is None:
        raise PermissionError("Job run_as verifier unavailable")
    service.identity.verify_run_as(executor.execution_ref, executor.principal_id)
    return service.scan(workspace_id, cursor)


def run_reapply(operation_id, *, facts, gate, identity, executor, enabled=False):
    if enabled is not True:
        raise PermissionError("VC reapply Job writes disabled")
    if str(UUID(operation_id)) != operation_id:
        raise ValueError("Canonical operation ID required")
    if executor.actor_kind != "service" or not executor.execution_ref.startswith("job/"):
        raise PermissionError("Reapply requires a target-local Job executor")
    identity.verify_run_as(executor.execution_ref, executor.principal_id)
    operation = facts.get_request(operation_id)
    request, approval = operation.request, operation.approval
    if (not isinstance(request, vc.MutationRequest) or request.operation_type != "reapply"
            or request.identity.operation_id != operation_id or request.binding.workspace_id != executor.workspace_id
            or approval is None or approval.status != vc.FactStatus.APPROVED or approval.approval_digest is None):
        raise PermissionError("Reapply requires a fresh approved immutable operation")
    inputs = approval.request.inputs
    if (inputs.operation_id != operation_id or inputs.operation_type != "reapply"
            or inputs.target_binding != request.binding or request.approval_id != approval.request.approval_id
            or inputs.expected_base_fingerprints.state_digest != request.expected_base
            or inputs.source_version_id != request.source_version_id):
        raise PermissionError("Reapply approval does not bind the new operation and base")
    return gate.execute(request, executor)


def main(argv=None):
    parser = argparse.ArgumentParser()
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--workspace-id")
    selection.add_argument("--operation-id")
    parser.add_argument("--cursor")
    args = parser.parse_args(argv)
    from backend.jobs import build_vc_runtime
    runtime = build_vc_runtime("reconcile")
    if args.operation_id:
        return run_reapply(args.operation_id, facts=runtime.facts, gate=runtime.gate,
                           identity=runtime.identity, executor=runtime.executor,
                           enabled=runtime.flags.enabled("vc_reapply_enabled"))
    return run_scan(args.workspace_id, args.cursor, service=runtime.service, executor=runtime.executor)


if __name__ == "__main__":
    main()
