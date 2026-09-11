"""Space-keyed VC bridge: the workbench is ``space_id``-keyed, the ledger is
binding-keyed. These endpoints resolve (or auto-enroll) the binding for a Genie space so
the SpaceDetail Version Control tab can list captured history and capture the current
state without knowing the internal binding id.

* ``GET /spaces/{space_id}/versions`` — history for the space (empty when not yet
  enrolled); read, gated on ``vc_history_enabled``.
* ``POST /spaces/{space_id}/observe`` — auto-enroll + capture the current state; write,
  gated on ``vc_writes_enabled``.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request

from backend.services.version_control import contracts as vc
from backend.services.version_control.observe_optimizer import resolve_or_enroll_bound

logger = logging.getLogger(__name__)

SpaceId = Annotated[str, Path(pattern=r"^[0-9a-zA-Z_-]{1,128}$")]


def _error(status, code, message, *, stale=False):
    return HTTPException(status, detail=vc.to_wire(vc.ApiError(
        code=code, message=message, retryable=False, stale=stale)))


def _invoke(call):
    try:
        return vc.to_wire(call())
    except HTTPException:
        raise
    except PermissionError as error:
        raise _error(403, "scope_denied", str(error)) from error
    except LookupError as error:
        raise _error(404, "resource_not_found", "Scoped resource not found") from error
    except (ValueError, TypeError) as error:
        raise _error(409, "request_conflict", str(error)) from error
    except Exception as error:
        # Fail closed to the client, but never silently: the server-side cause is the
        # only signal for an opaque evidence_unavailable 503.
        logger.exception("VC space request failed")
        raise _error(503, "evidence_unavailable", "Evidence unavailable", stale=True) from error


def build_router(*, runtime):
    router = APIRouter(prefix="/api/version-control")
    ledger, registry, identity = runtime.ledger, runtime.registry, runtime.identity
    flags, authorize_history = runtime.flags, runtime.authorize_history

    def actor_for(request):
        authentication = getattr(request.state, "vc_auth", None)
        if authentication is None:
            raise _error(401, "authentication_required", "Authentication required")
        return identity.actor(authentication)

    @router.get("/spaces/{space_id}/versions")
    def space_versions(space_id: SpaceId, request: Request,
                       limit: Annotated[int, Query(ge=1, le=100)] = 25,
                       cursor: str | None = None):
        def read():
            if flags.enabled("vc_history_enabled") is not True:
                raise _error(503, "vc_history_disabled", "VC history reads disabled", stale=True)
            actor = actor_for(request)
            binding = registry.find_active_by_space_key(space_id)
            if binding is None:
                return vc.VersionPage((), None)  # not enrolled yet -> empty history
            if (actor.workspace_id != binding.workspace_id
                    or authorize_history(actor, binding) is not True):
                raise PermissionError("Binding history scope denied")
            return ledger.history(binding, cursor, limit)
        return _invoke(read)

    @router.post("/spaces/{space_id}/observe")
    def space_observe(space_id: SpaceId, request: Request):
        def capture():
            if flags.enabled("vc_writes_enabled") is not True:
                raise _error(503, "vc_writes_disabled", "VC writes disabled", stale=True)
            actor = actor_for(request)
            binding = resolve_or_enroll_bound(runtime, space_id=space_id)
            if authorize_history(actor, binding) is not True:
                raise PermissionError("Binding history scope denied")
            return runtime.observer.capture_on_open(binding, actor)
        return _invoke(capture)

    return router
