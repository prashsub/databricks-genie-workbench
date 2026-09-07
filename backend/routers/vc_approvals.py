"""M08 supplies authenticated request context and explicitly mounts this router."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from backend.services.version_control.contracts import (
    ApprovalInputs, BreakGlassRequest, OperationHandle, OperationStatus, from_wire, to_wire,
)


class VoteBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approve', 'reject']


def authenticated_actor(request, identity):
    authentication = getattr(request.state, 'vc_auth', None)
    if authentication is None:
        raise HTTPException(401, detail={'code': 'authentication_required', 'message': 'Authentication required',
                                         'retryable': False, 'stale': False})
    return identity.actor(authentication)


def parse_inputs(value_type, payload):
    try:
        return from_wire(value_type, payload)
    except (ValueError, TypeError) as error:
        raise HTTPException(422, detail={'code': 'invalid_inputs', 'message': str(error),
                                         'retryable': False, 'stale': False}) from error


def invoke(call):
    try:
        return to_wire(call())
    except HTTPException:
        raise
    except (PermissionError, LookupError, ValueError, TypeError) as error:
        status = 403 if isinstance(error, PermissionError) else 404 if isinstance(error, LookupError) else 409
        raise HTTPException(status, detail={'code': 'governance_rejected', 'message': str(error),
                                            'retryable': False, 'stale': False}) from error
    except Exception as error:
        raise HTTPException(503, detail={'code': 'evidence_unavailable', 'message': 'Poll operation; do not replay mutation',
                                         'retryable': False, 'stale': True}) from error


def build_router(service, identity):
    router = APIRouter(prefix='/api/vc')

    @router.post('/approvals')
    def request_approval(body: dict, request: Request):
        def submit():
            actor = authenticated_actor(request, identity)
            return service.request(parse_inputs(ApprovalInputs, body), actor)
        return invoke(submit)

    @router.post('/approvals/{approval_id}/votes')
    def vote(approval_id: str, body: VoteBody, request: Request):
        return invoke(lambda: service.vote(approval_id, body.decision, authenticated_actor(request, identity)))

    @router.get('/approvals/{approval_id}')
    def get(approval_id: str, request: Request):
        def read():
            actor = authenticated_actor(request, identity)
            record = service.get(approval_id)
            binding = record.request.inputs.target_binding
            if actor.workspace_id != binding.workspace_id or not (
                    identity.can_edit(actor.subject_id, binding)
                    or 'approvers' in identity.groups(actor.subject_id, binding.workspace_id)):
                raise PermissionError('Approval read scope denied')
            return record
        return invoke(read)

    @router.post('/break-glass', status_code=202)
    def break_glass(body: dict, request: Request):
        def override():
            actor = authenticated_actor(request, identity)
            scoped = parse_inputs(BreakGlassRequest, body)
            service.break_glass(scoped, actor)
            return OperationHandle(scoped.identity.operation_id, OperationStatus.QUARANTINED, None)
        return invoke(override)

    return router
