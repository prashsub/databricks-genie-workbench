"""Cursor polling over a target-local operations fact projection, never a queue."""

from backend.services.version_control import contracts as vc
from .releases import ADMISSION_REFERENCE


class PendingOperations:
    def __init__(self, read_page):
        self.read_page = read_page

    def scan(self, workspace_id, cursor):
        page = self.read_page(workspace_id, cursor)
        return vc.DispatchPage(tuple(vc.OperationHandle(fact.operation_id,
            vc.OperationStatus(fact.status.value), fact.job_run_id) for fact in page.items
            if fact.binding.workspace_id == workspace_id and fact.fact_kind == vc.FactKind.OPERATION
            and fact.operation_type == 'promotion' and fact.status == vc.FactStatus.REQUESTED
            and fact.approval_reference == ADMISSION_REFERENCE), page.next_cursor)
