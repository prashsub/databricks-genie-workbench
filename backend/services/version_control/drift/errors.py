"""Fail-closed command errors; an operation ID is for polling, never replay."""

from backend.services.version_control import contracts as vc


class ReconcileError(RuntimeError):
    def __init__(self, code, message, *, http_status=503, operation_id=None, stale=True):
        super().__init__(message)
        self.http_status = http_status
        self.error = vc.ApiError(code=code, message=message, operation_id=operation_id,
                                 retryable=False, stale=stale)
