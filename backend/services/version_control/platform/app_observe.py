"""Fail-closed app wiring for the observe-only VC surface.

``main`` calls these to (1) derive the observe runtime config from the app
environment (returning ``None`` — no surface — unless the trusted-workspace
control-plane vars are all set), (2) mount the reads-plus-capture routers, and
(3) derive the per-request ``vc_auth`` from the Databricks Apps forwarded-identity
headers. Everything is fail-closed: without config, ``main`` mounts nothing and
behaves exactly as before (deploy-only validated per the install-path contract).

Restore/reconcile mutations are NOT wired here (reads-only scope). ``FailClosedRestore``
satisfies the ``vc_mutations`` router contract while guaranteeing a restore submit can
never execute until a real governed-write restore service is wired and platform-checked.
"""

from typing import Any

from backend.services.version_control import contracts as vc
from backend.services.version_control.platform.observe_seams import ObserveRuntime

_REQUIRED_ENV = (
    "VC_OBSERVE_WORKSPACE_ID",
    "VC_OBSERVE_CATALOG",
    "VC_OBSERVE_CONTROL_SCHEMA",
    "VC_OBSERVE_WAREHOUSE_ID",
    "VC_OBSERVE_HOST",
    "VC_OBSERVE_SP_PRINCIPAL_ID",
)


def _flag(env: Any, name: str) -> bool:
    return str(env.get(name, "")).strip().lower() == "true"


def observe_config_from_env(env: Any) -> dict | None:
    """Assemble the observe runtime config from env, or ``None`` if incomplete.

    On Databricks Apps the app SP is the ambient identity, so ``roles.executor`` carries
    no profile/auth block and ``PlatformAdapters`` builds an ambient ``WorkspaceClient``.
    """
    if any(not env.get(key) for key in _REQUIRED_ENV):
        return None
    warehouse_id = env["VC_OBSERVE_WAREHOUSE_ID"]
    host = env["VC_OBSERVE_HOST"]
    workspace_id = env["VC_OBSERVE_WORKSPACE_ID"]
    profile = env.get("VC_OBSERVE_PROFILE") or None
    selection = {
        "workspace_id": workspace_id,
        "host": host,
        "principal_id": env["VC_OBSERVE_SP_PRINCIPAL_ID"],
        "execution_ref": env.get("VC_OBSERVE_EXECUTION_REF") or "app/genie-workbench",
        "profile": profile,
    }
    return {
        "workspace_id": workspace_id,
        "catalog": env["VC_OBSERVE_CATALOG"],
        "control_schema": env["VC_OBSERVE_CONTROL_SCHEMA"],
        "warehouse_id": warehouse_id,
        "target_warehouse_id": warehouse_id,
        "target_selection": selection,
        "environment": env.get("VC_OBSERVE_ENVIRONMENT") or "prod",
        "roles": {"executor": {"warehouse_id": warehouse_id, "host": host, "profile": profile}},
        "flags": {
            "vc_history_enabled": _flag(env, "VC_HISTORY_ENABLED"),
            "vc_writes_enabled": _flag(env, "VC_WRITES_ENABLED"),
            "vc_restore_enabled": _flag(env, "VC_RESTORE_ENABLED"),
        },
    }


def vc_auth_from_request(headers: Any, workspace_id: str, *, dev_email: str | None = None):
    """Derive ``AuthenticatedRequest`` from Databricks Apps forwarded-identity headers.

    Returns ``None`` when no authenticated subject is present (the routers then fail
    closed with 401). Reads are proxied through the trusted target workspace, so the
    workspace_id is the observe runtime's, not the caller's.
    """
    email = (headers.get("X-Forwarded-User") or headers.get("X-Forwarded-Email")
             or dev_email)
    if not email:
        return None
    return vc.AuthenticatedRequest(email, workspace_id)


class FailClosedRestore:
    """Satisfies the ``vc_mutations`` restore port while never executing a mutation."""

    def submit(self, _operation_id: str, _actor: Any) -> Any:
        raise PermissionError("Restore is not wired in the observe-only surface")


def mount_observe_routers(app: Any, runtime: ObserveRuntime) -> None:
    """Mount the reads-plus-capture routers (history + observe/restore) from the runtime.

    Restore stays fail-closed (flags default off + ``FailClosedRestore``); the observe
    capture and history reads are the live surface.
    """
    from backend.routers.vc_history import build_router as build_history_router
    from backend.routers.vc_mutations import build_router as build_mutations_router

    app.include_router(build_history_router(
        ledger=runtime.ledger, registry=runtime.registry, identity=runtime.identity,
        authorize_history=runtime.authorize_history, flags=runtime.flags))
    app.include_router(build_mutations_router(
        observer=runtime.observer, restore=FailClosedRestore(), identity=runtime.identity,
        registry=runtime.registry, facts=runtime.facts,
        authorize_history=runtime.authorize_history,
        reader_selection=runtime.reader_selection, flags=runtime.flags))
