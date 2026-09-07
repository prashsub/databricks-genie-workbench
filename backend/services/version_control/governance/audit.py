"""Optional, asynchronously fetched platform evidence; never authorization."""

from datetime import datetime, timedelta

from backend.services.version_control.contracts import FactKind, OperationFacts, IdentityProvider, to_wire


class AuditService:
    def __init__(self, facts: OperationFacts, identity: IdentityProvider, now, fetch):
        self.facts = facts
        self.identity = identity
        self.now = now
        self.fetch = fetch

    def _scoped(self, operation_id, actor):
        stored = self.facts.get_request(operation_id)
        binding = stored.request.binding
        if actor.workspace_id != binding.workspace_id or not self.identity.can_edit(actor.subject_id, binding):
            raise PermissionError('Operation evidence read scope denied')
        history = self.facts.lookup_request(binding, stored.request.identity.idempotency_key)
        if history.ambiguous or not history.facts:
            raise ValueError('Ambiguous or missing operation evidence')
        return stored, history

    def get(self, operation_id, actor):
        stored, history = self._scoped(operation_id, actor)
        checkpoints = [row for row in history.facts if row.fact_kind in (FactKind.OPERATION, FactKind.CREATE_INTENT)]
        return {
            'operation': to_wire(stored.request),
            'status': checkpoints[-1].status.value if checkpoints else 'requested',
            'checkpoints': to_wire(checkpoints),
            'receipt_references': [row.event_id for row in history.facts if row.fact_kind == FactKind.RECEIPT],
            'audit': {'availability': 'pending', 'authoritative': False, 'actor': 'unknown'},
        }

    async def correlate(self, operation_id, actor):
        stored, history = self._scoped(operation_id, actor)
        binding = stored.request.binding
        start = min(row.recorded_at for row in history.facts) - timedelta(minutes=5)
        end = self.now() + timedelta(minutes=5)
        result = {'availability': 'lagging', 'authoritative': False, 'actor': 'unknown', 'events': []}
        try:
            events = await self.fetch(binding, operation_id, start, end)
            scoped = [event for event in events if event.get('workspace_id') == binding.workspace_id
                      and event.get('space_id') == binding.space_id
                      and event.get('operation_id') == operation_id
                      and start <= datetime.fromisoformat(event['recorded_at'].replace('Z', '+00:00')) <= end]
            result['events'] = [{**event, 'actor_id': event.get('actor_id') or 'unknown'} for event in scoped]
            if scoped:
                result['availability'] = 'available'
        except Exception:
            result['availability'] = 'unavailable'
        return result
