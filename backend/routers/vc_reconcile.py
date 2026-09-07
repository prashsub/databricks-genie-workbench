"""M05 authenticated projection and command routes; M08 owns mounting."""

from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.services.version_control import contracts as vc


class ReconcileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["adopt", "reapply", "acknowledge"]
    binding_revision: int = Field(gt=0)
    expected_base: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_id: UUID | None = None
    policy_inputs: dict = Field(default_factory=dict)


def _invoke(call):
    try:
        return vc.to_wire(call())
    except HTTPException:
        raise
    except PermissionError as error:
        code, status, stale = "scope_denied", 403, False
        message = str(error)
    except LookupError:
        code, status, stale = "resource_not_found", 404, False
        message = "Scoped resource not found"
    except (ValueError, TypeError) as error:
        code, status, stale = "request_conflict", 409, False
        message = str(error)
    except Exception:
        code, status, stale = "evidence_unavailable", 503, True
        message = "Evidence unavailable; do not replay mutation"
    raise HTTPException(status, detail=vc.to_wire(vc.ApiError(
        code=code, message=message, retryable=False, stale=stale)))


def build_router(*, service, identity, registry, authorize_history):
    router = APIRouter(prefix="/api/version-control")

    def actor_for(request):
        authentication = getattr(request.state, "vc_auth", None)
        if authentication is None:
            raise HTTPException(401, detail=vc.to_wire(vc.ApiError(
                code="authentication_required", message="Authentication required", retryable=False, stale=False)))
        return identity.actor(authentication)

    def scope(request, binding_id):
        actor = actor_for(request)
        binding = registry.resolve(str(binding_id))
        if (binding.binding_id != str(binding_id) or actor.workspace_id != binding.workspace_id
                or authorize_history(actor, binding) is not True):
            raise PermissionError("Binding history scope denied")
        return actor, binding

    @router.get("/overview")
    def overview(request: Request, cursor: str | None = None,
                 limit: Annotated[int, Query(ge=1, le=100)] = 100):
        return _invoke(lambda: service.overview(actor_for(request), cursor, limit))

    @router.get("/bindings/{binding_id}/status")
    def status(binding_id: UUID, request: Request):
        def read():
            actor, binding = scope(request, binding_id)
            return service.status(binding, actor)
        return _invoke(read)

    @router.post("/bindings/{binding_id}/reconcile", status_code=202)
    def reconcile(binding_id: UUID, body: ReconcileBody, request: Request,
                  idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)]):
        def submit():
            actor, binding = scope(request, binding_id)
            payload = body.model_dump(mode="json")
            digest = vc.canonical_json_hash("vc-reconcile-request/1", {
                "binding": vc.to_wire(binding), "actor": vc.to_wire(actor), "body": payload})
            key = vc.canonical_json_hash("vc-reconcile-key/1", {
                "workspace_id": actor.workspace_id, "binding_id": binding.binding_id,
                "actor": actor.subject_id, "key": idempotency_key})
            operation_id = str(uuid5(NAMESPACE_URL, key))
            action = vc.ReconcileRequest(vc.RequestIdentity(operation_id, idempotency_key, digest),
                                         body.action, body.binding_revision, body.expected_base,
                                         str(body.approval_id) if body.approval_id else None, body.policy_inputs)
            return service.reconcile(binding, action, actor)
        return _invoke(submit)

    return router
