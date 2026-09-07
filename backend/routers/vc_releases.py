"""Authenticated release routes; mounting remains an M08 deployment decision."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.services.version_control import contracts as vc


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_version_id: UUID
    mapping: dict
    policy: dict
    source_profile: str = Field(min_length=1)
    target_profile: str = Field(min_length=1)
    expected_base: str = Field(pattern=r'^[0-9a-f]{64}$')


class PromoteBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approval_id: UUID


def invoke(call):
    try:
        return vc.to_wire(call())
    except HTTPException:
        raise
    except PermissionError as error:
        status, code, message, stale = 403, 'promotion_denied', str(error), False
    except LookupError:
        status, code, message, stale = 404, 'release_not_found', 'Scoped release not found', False
    except (ValueError, TypeError) as error:
        status, code, message, stale = 409, 'promotion_conflict', str(error), False
    except Exception:
        status, code, message, stale = 503, 'evidence_unavailable', 'Poll the operation; do not replay mutation', True
    raise HTTPException(status, detail=vc.to_wire(vc.ApiError(code=code, message=message, retryable=False, stale=stale)))


def build_router(*, commands, identity):
    router = APIRouter(prefix='/api/version-control')

    def actor(request):
        authentication = getattr(request.state, 'vc_auth', None)
        if authentication is None:
            raise HTTPException(401, detail=vc.to_wire(vc.ApiError(code='authentication_required',
                message='Server authentication required', retryable=False, stale=False)))
        return identity.actor(authentication)

    @router.post('/releases', status_code=202)
    def create(body: ReleaseBody, request: Request,
               key: Annotated[str, Header(alias='Idempotency-Key', min_length=1)]):
        def submit():
            requester = actor(request)
            try:
                mapping = vc.from_wire(vc.MappingSpec, body.mapping)
                policy = vc.from_wire(vc.TestPolicy, body.policy)
            except (ValueError, TypeError) as error:
                raise HTTPException(422, detail=str(error)) from error
            return commands.create(str(body.source_version_id), mapping, policy, body.expected_base,
                                   body.source_profile, body.target_profile, key, requester)
        return invoke(submit)

    @router.post('/releases/{release_id}/validate', status_code=202)
    def validate(release_id: UUID, request: Request,
                 key: Annotated[str, Header(alias='Idempotency-Key', min_length=1)]):
        return invoke(lambda: commands.validate(str(release_id), key, actor(request)))

    @router.post('/releases/{release_id}/promote', status_code=202)
    def promote(release_id: UUID, body: PromoteBody, request: Request,
                key: Annotated[str, Header(alias='Idempotency-Key', min_length=1)]):
        return invoke(lambda: commands.promote(str(release_id), str(body.approval_id), key, actor(request)))

    @router.get('/releases/{release_id}/receipt')
    def receipt(release_id: UUID, request: Request, response: Response):
        value = invoke(lambda: commands.receipt(str(release_id), actor(request)))
        if value is None:
            response.status_code = 202
            return vc.to_wire(vc.OperationHandle(str(release_id), vc.OperationStatus.REQUESTED, None))
        return value

    return router
