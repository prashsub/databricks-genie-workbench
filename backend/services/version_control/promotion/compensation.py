"""Only the M04 guarded restore protocol may compensate a promotion."""

from backend.services.version_control import contracts as vc


def attempt(service, operation, receipt, terminal, executor):
    policy = operation.approval.request.inputs.recovery_policy
    compensation_id = policy.get('compensation_operation_id')
    if (not compensation_id or compensation_id == receipt.operation_id
            or operation.approval.request.requested_at >= terminal.recorded_at
            or terminal.status != vc.FactStatus.CONFIRMED or receipt.post_version_id is None):
        return receipt
    history = service.facts.lookup_request(operation.request.binding, operation.request.identity.idempotency_key)
    if history.ambiguous:
        return receipt
    compensation = service.facts.get_request(compensation_id).request
    if (compensation.operation_type != 'restore' or compensation.binding != receipt.target_binding
            or compensation.identity.request_digest != policy.get('compensation_request_digest')
            or compensation.source_version_id != receipt.pre_version_id
            or compensation.expected_base != receipt.observed_fingerprints.state_digest):
        return receipt
    observed = service.observer.capture(receipt.target_binding, 'promotion_compensation', executor)
    if (observed.busy or observed.status.stale or observed.status.quarantined or observed.captured_version is None
            or observed.captured_version.fingerprints != receipt.observed_fingerprints):
        return receipt
    result = service.restore.compensate(receipt.operation_id, compensation_id, executor)
    from dataclasses import replace
    return replace(receipt, compensation_operation_id=compensation_id,
        status=vc.OperationStatus.COMPENSATED if result.status in {vc.OperationStatus.CONFIRMED, vc.OperationStatus.NOOP}
        and not result.unresolved else vc.OperationStatus.FAILED)
