#!/usr/bin/env python3
"""Provision the live approval-gate identities for the M06/M08 audit deployment gate.

Idempotently creates, in the TARGET workspace, the identities the production
approval flow re-checks at authorization time:

  * two distinct human users (SCIM ``User`` records -> ``actor_kind == 'human'``)
    that act as the non-requester approvers,
  * the policy groups ``approvers`` / ``target-approvers`` (approver membership,
    re-resolved target-side) and ``release-requesters`` (release/promotion
    requester right),
  * the two users added to ``approvers`` + ``target-approvers`` and the requester
    service principal added to ``release-requesters``,
  * a Job whose ``run_as`` is the executor service principal, so
    ``verify_run_as(execution_ref, executor_principal)`` has a real subject.

Everything is admin authority (SCIM + Jobs); the provisioner/runtime SPs are not
used here. Run with the workspace-admin profile:

    uv run python scripts/version_control/provision_approval_identities.py \
        --admin-profile fevm-serverless

It prints the resolved ids and writes them back into VC_INTEGRATION_CONFIG under
``approval_identities`` so the m06 fixture can build the live ApprovalService.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from databricks.sdk import WorkspaceClient

APPROVER_USERS = ("vc-approver-1@example.com", "vc-approver-2@example.com")
GROUPS = ("approvers", "target-approvers", "release-requesters")


def _ensure_group(client: WorkspaceClient, display_name: str) -> str:
    for group in client.groups.list():
        if group.display_name == display_name:
            return group.id
    created = client.groups.create(display_name=display_name)
    print(f"  created group {display_name} -> {created.id}")
    return created.id


def _ensure_user(client: WorkspaceClient, user_name: str) -> str:
    for user in client.users.list():
        if user.user_name == user_name:
            return user.id
    created = client.users.create(user_name=user_name, active=True,
                                  display_name=user_name.split("@")[0])
    print(f"  created user {user_name} -> {created.id}")
    return created.id


def _member_ids(client: WorkspaceClient, group_id: str) -> set[str]:
    group = client.groups.get(group_id)
    return {member.value for member in (group.members or [])}


def _add_member(client: WorkspaceClient, group_id: str, subject_id: str) -> None:
    if subject_id in _member_ids(client, group_id):
        return
    from databricks.sdk.service.iam import ComplexValue, Patch, PatchOp, PatchSchema

    client.groups.patch(
        group_id,
        operations=[Patch(op=PatchOp.ADD, path="members",
                          value=[ComplexValue(value=subject_id).as_dict()])],
        schemas=[PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP])
    print(f"  added member {subject_id} -> group {group_id}")


def _grant_job_view(client: WorkspaceClient, job_id: int, executor_principal: str) -> None:
    # The executor SP reads its own run_as job to satisfy verify_run_as; give it
    # CAN_VIEW so the target-local job reader needs no admin credential.
    from databricks.sdk.service.jobs import JobAccessControlRequest, JobPermissionLevel

    client.jobs.update_permissions(job_id=str(job_id), access_control_list=[
        JobAccessControlRequest(service_principal_name=executor_principal,
                                permission_level=JobPermissionLevel.CAN_VIEW)])
    print(f"  granted CAN_VIEW on job {job_id} -> {executor_principal}")


def _ensure_run_as_job(client: WorkspaceClient, executor_principal: str) -> str:
    name = "vc-audit-executor-run-as"
    for job in client.jobs.list(name=name):
        settings = job.settings
        run_as = getattr(settings, "run_as", None)
        if getattr(run_as, "service_principal_name", None) != executor_principal:
            # Repoint an existing job whose run_as drifted.
            client.jobs.reset(job_id=job.job_id, new_settings=_run_as_settings(name, executor_principal))
            print(f"  repointed job {job.job_id} run_as -> {executor_principal}")
        _grant_job_view(client, job.job_id, executor_principal)
        return str(job.job_id)
    created = client.jobs.create(**_run_as_settings(name, executor_principal, as_dict=True))
    print(f"  created run_as job -> {created.job_id}")
    _grant_job_view(client, created.job_id, executor_principal)
    return str(created.job_id)


def _run_as_settings(name, executor_principal, as_dict=False):
    from databricks.sdk.service.jobs import JobRunAs, JobSettings, NotebookTask, Task

    # A never-run placeholder Job; verify_run_as only reads settings.run_as.
    task = Task(task_key="noop",
                notebook_task=NotebookTask(notebook_path="/Shared/vc-audit-noop"),
                timeout_seconds=1)
    run_as = JobRunAs(service_principal_name=executor_principal)
    if as_dict:
        return {"name": name, "tasks": [task], "run_as": run_as,
                "max_concurrent_runs": 1}
    return JobSettings(name=name, tasks=[task], run_as=run_as, max_concurrent_runs=1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-profile", required=True)
    parser.add_argument("--config", default=os.environ.get("VC_INTEGRATION_CONFIG"))
    args = parser.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text())
    client = WorkspaceClient(profile=args.admin_profile)

    print("groups:")
    group_ids = {name: _ensure_group(client, name) for name in GROUPS}

    print("users:")
    user_ids = {name: _ensure_user(client, name) for name in APPROVER_USERS}

    print("membership:")
    for user_id in user_ids.values():
        _add_member(client, group_ids["approvers"], user_id)
        _add_member(client, group_ids["target-approvers"], user_id)
    requester = config["roles"]["enrollment"]["principal_id"]
    _add_member(client, group_ids["release-requesters"], requester)

    print("run_as job:")
    executor_principal = config["roles"]["executor"]["principal_id"]
    execution_ref = _ensure_run_as_job(client, executor_principal)

    config["approval_identities"] = {
        "approvers": [{"user_name": name, "id": user_ids[name]} for name in APPROVER_USERS],
        "requester_principal_id": requester,
        "groups": group_ids,
        "execution_ref": execution_ref,
        # Target-local directory reader for live SCIM group/actor resolution
        # (service principals cannot read arbitrary Users/Groups; the M08 identity
        # provider is granted directory read — modelled here by the admin profile).
        "directory_profile": args.admin_profile,
    }
    config_path.write_text(json.dumps(config, indent=2))
    print(f"\nwrote approval_identities to {config_path}")
    print(json.dumps(config["approval_identities"], indent=2))


if __name__ == "__main__":
    main()
