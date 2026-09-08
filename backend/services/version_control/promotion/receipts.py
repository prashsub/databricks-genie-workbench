"""Target Delta receipts bind gate lineage and authoritative GET versions."""

from dataclasses import replace
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from backend.services.version_control import contracts as vc
from backend.services.version_control.governance.facts import fact_key
from .mapping import RenderedPayload
from .packages import digest, encode, immutable_put
from . import compensation


def existing(history):
    found = [fact.evidence for fact in history.facts if fact.fact_kind == vc.FactKind.RECEIPT]
    if history.ambiguous or any(not isinstance(item, vc.DeploymentReceipt) for item in found):
        raise ValueError('Ambiguous receipt history')
    if found and any(item != found[0] for item in found):
        raise ValueError('Conflicting durable receipts')
    return found[0] if found else None


def export(service, receipt):
    prefix = f'{service.receipt_volume}/sha256/{digest(receipt)}'
    evidence = {'validation_evidence_digest': receipt.validation_evidence_digest,
                'benchmark_evidence_digest': receipt.benchmark_evidence_digest}
    immutable_put(service.receipt_store, prefix + '/tests.json', encode(evidence))
    immutable_put(service.receipt_store, prefix + '/receipt.json', encode(receipt))
    return receipt


def record(service, operation, release, manifest, rendered, policy, result, executor):
    request = operation.request
    history = service.facts.lookup_request(request.binding, request.identity.idempotency_key)
    if history.ambiguous or result.operation_id != request.identity.operation_id:
        raise ValueError('Ambiguous gate result cannot become a receipt')
    facts = [fact for fact in history.facts if fact.fact_kind == vc.FactKind.OPERATION
             and fact.operation_id == result.operation_id]
    if not facts or not isinstance(result.preimage, vc.ObservationRef):
        raise ValueError('Gate lineage and committed target preimage required')
    terminal = max(facts, key=lambda fact: fact.transition_sequence)
    if terminal.attempt_id is None or terminal.generation is None:
        raise ValueError('Receipt requires durable gate attempt and fence')
    pre = service.ledger.get_version(request.binding, result.preimage.version_id)
    post = service.ledger.get_version(request.binding, result.postimage.version_id) if result.postimage else pre
    if (pre.context.binding != request.binding or post.context.binding != request.binding
            or pre.snapshot.state_digest != result.preimage.state_digest
            or (result.postimage and post.snapshot.state_digest != result.postimage.state_digest)):
        raise ValueError('Authoritative target version mismatch')
    snapshot = service.canonicalizer.observe({'serialized_space': rendered.serialized_space,
                                              'description': rendered.description})
    confirmed = result.status in {vc.OperationStatus.CONFIRMED, vc.OperationStatus.NOOP} and not result.unresolved
    if confirmed and service.canonicalizer.compare(snapshot, post.snapshot) != vc.Comparison.EQUAL:
        raise ValueError('Target GET does not match rendered intent')
    tests = service.tests.run(RenderedPayload(vc.to_wire(post.snapshot.serialized_space),
        post.snapshot.restorable_metadata.get('description'), rendered.environment), policy, executor) if confirmed else {
            'validation': {'passed': False, 'reason': result.status.value},
            'benchmark': {'passed': False, 'reason': result.status.value}}
    status = result.status
    if confirmed and any(tests[kind].get('passed') is not True for kind in ('validation', 'benchmark')):
        status = vc.OperationStatus.FAILED
    receipt = vc.DeploymentReceipt(schema_version='VC/1.0', release_id=release.release_id,
        operation_id=result.operation_id, target_binding=request.binding, attempt_id=terminal.attempt_id,
        generation=terminal.generation, approval_id=operation.approval.request.approval_id,
        approval_digest=operation.approval.approval_digest, source_version_id=manifest.source_version_id,
        package_digest=manifest.package_digest, mapping_digest=manifest.mapping_digest,
        intended_fingerprints=manifest.source_fingerprints, rendered_fingerprints=snapshot.fingerprints,
        observed_fingerprints=post.snapshot.fingerprints, pre_version_id=result.preimage.version_id,
        post_version_id=result.postimage.version_id if result.postimage else None,
        transformer_version=manifest.transformer_version, canonicalizer_version=snapshot.fingerprints.canonicalizer_version,
        executor_id=executor.principal_id, job_run_id=terminal.job_run_id or executor.execution_ref,
        validation_evidence_digest=digest(tests['validation']), benchmark_evidence_digest=digest(tests['benchmark']),
        status=status, compensation_operation_id=None, recorded_at=datetime.now(timezone.utc))
    if confirmed and status == vc.OperationStatus.FAILED:
        receipt = compensation.attempt(service, operation, receipt, terminal, executor)
    fact = replace(terminal, fact_kind=vc.FactKind.RECEIPT, evidence=receipt, status=vc.FactStatus(receipt.status.value),
        release_id=release.release_id, recorded_at=receipt.recorded_at, transition_sequence=0)
    key = fact_key(fact)
    fact = replace(fact, event_id=str(uuid5(NAMESPACE_URL, key)), event_key=key)
    service.facts.append(fact)
    committed = service.facts.lookup_request(request.binding, request.identity.idempotency_key)
    if committed.ambiguous or not any(row == fact for row in committed.facts):
        raise ValueError('Receipt publication unconfirmed; poll without replaying mutation')
    return receipt
