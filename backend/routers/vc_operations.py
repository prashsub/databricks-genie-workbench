"""Scoped operation reads. M08 owns router composition and authentication."""

from fastapi import APIRouter, HTTPException, Request

from .vc_approvals import authenticated_actor, invoke


def build_router(audit, identity):
    router = APIRouter(prefix='/api/vc/operations')

    @router.get('/{operation_id}')
    def get(operation_id: str, request: Request):
        return invoke(lambda: audit.get(operation_id, authenticated_actor(request, identity)))

    @router.get('/{operation_id}/audit')
    async def correlate(operation_id: str, request: Request):
        actor = invoke(lambda: authenticated_actor(request, identity))
        from backend.services.version_control.contracts import ActorContext, from_wire

        try:
            return await audit.correlate(operation_id, from_wire(ActorContext, actor))
        except PermissionError as error:
            raise HTTPException(403, detail={'code': 'evidence_scope_denied', 'retryable': False, 'stale': False}) from error
        except Exception as error:
            raise HTTPException(503, detail={'code': 'evidence_unavailable', 'retryable': False, 'stale': True}) from error

    return router
