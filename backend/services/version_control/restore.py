"""Target-local historical restore orchestrated through approved immutable requests."""

from backend.services.version_control import contracts as vc


class RestoreService:
    def __init__(self, *, facts, ledger, gate, dispatcher, identity, flags, validate):
        self.facts = facts
        self.ledger = ledger
        self.gate = gate
        self.dispatcher = dispatcher
        self.identity = identity
        self.flags = flags
        self.validate = validate

    def submit(self, operation_id, actor):
        request = self._load(operation_id)
        if (actor.workspace_id != request.binding.workspace_id
                or self.identity.can_edit(actor.subject_id, request.binding) is not True):
            raise PermissionError("Restore requester is not authorized for this target")
        return self.dispatcher.submit_local(operation_id, "restore")

    def run(self, operation_id, executor):
        request = self._load(operation_id)
        if (executor.workspace_id != request.binding.workspace_id
                or executor.actor_kind != "service" or not executor.execution_ref.startswith("job/")):
            raise PermissionError("Restore requires a target-local Job executor")
        self.identity.verify_run_as(executor.execution_ref, executor.principal_id)
        return self.gate.execute(request, executor)

    def _load(self, operation_id):
        if self.flags.enabled("vc_restore_enabled") is not True:
            raise PermissionError("VC restore writes disabled")
        request = self.facts.get_request(operation_id).request
        if (not isinstance(request, vc.MutationRequest) or request.operation_type != "restore"
                or request.identity.operation_id != operation_id):
            raise PermissionError("Expected immutable restore request")
        version = self.ledger.get_version(request.binding, request.source_version_id)
        if (version.context.binding != request.binding
                or version.snapshot.serialized_space != request.serialized_space
                or version.snapshot.restorable_metadata.get("description") != request.description
                or self.validate(version, request) is not True):
            raise ValueError("Historical schema, dependencies or approved payload invalid")
        return request
