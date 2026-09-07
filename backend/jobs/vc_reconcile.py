"""Target-local scheduled observation; no mutation implementation or fallback."""

import argparse


def run_scan(workspace_id, cursor, *, service, executor):
    if (executor.workspace_id != workspace_id or executor.actor_kind != "service"
            or not executor.execution_ref.startswith("job/") or service.executor != executor):
        raise PermissionError("Reconciliation scan requires its target-local Job executor")
    if service.identity is None:
        raise PermissionError("Job run_as verifier unavailable")
    service.identity.verify_run_as(executor.execution_ref, executor.principal_id)
    return service.scan(workspace_id, cursor)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--cursor")
    args = parser.parse_args(argv)
    from backend.jobs import build_vc_runtime
    runtime = build_vc_runtime("reconcile")
    return run_scan(args.workspace_id, args.cursor, service=runtime.service, executor=runtime.executor)


if __name__ == "__main__":
    main()
