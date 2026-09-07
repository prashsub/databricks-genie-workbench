"""Server-side identity verification without authority repair or credential fallback."""

from urllib.parse import urlsplit

from ..contracts import ExecutorContext


def canonical_host(host: str) -> str:
    parsed = urlsplit(host)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.port):
        raise ValueError("Explicit HTTPS workspace host required")
    return f"https://{parsed.hostname.lower()}"


class PlatformIdentityProvider:
    def __init__(self, *, profiles=None, obo_executors=None, request_resolver=None,
                 group_resolver=None, edit_resolver=None, job_client=None):
        self._profiles = dict(profiles or {})
        self._obo_executors = dict(obo_executors or {})
        self._request_resolver = request_resolver
        self._group_resolver = group_resolver
        self._edit_resolver = edit_resolver
        self._job_client = job_client
        self._contexts = {}

    def executor(self, selection) -> ExecutorContext:
        expected_host = canonical_host(selection.host)
        if not all((selection.workspace_id, selection.principal_id, selection.execution_ref)):
            raise ValueError("Explicit workspace, principal and execution required")
        if selection.profile is None:
            factory = self._obo_executors.get(selection.execution_ref)
            allowed_auth = {"pat"}
            actor_kind = "user"
        else:
            if not selection.profile.strip() or selection.profile.upper() == "DEFAULT":
                raise PermissionError("Default profile is not an explicit VC identity")
            factory = self._profiles.get(selection.profile)
            allowed_auth = {"oauth-m2m"}
            actor_kind = "service_principal"
        if factory is None:
            raise PermissionError("No explicitly bound executor credentials")
        client = factory()
        if client.config.auth_type not in allowed_auth:
            raise PermissionError("Executor must use explicitly bound OAuth M2M or OBO")
        self._verify_client(client, expected_host, selection.workspace_id, selection.principal_id)
        context = ExecutorContext(selection.workspace_id, expected_host, selection.principal_id,
                                  actor_kind, client, selection.execution_ref)
        self._contexts[id(context)] = context
        return context

    @staticmethod
    def _verify_client(client, host, workspace_id, principal_id):
        if canonical_host(client.config.host) != host or str(client.get_workspace_id()) != workspace_id:
            raise PermissionError("Executor host/workspace mismatch")
        identity = client.current_user.me()
        actual = getattr(identity, "application_id", None) or getattr(identity, "id", None)
        if actual != principal_id:
            raise PermissionError("Executor principal mismatch")

    def _client(self, executor):
        if self._contexts.get(id(executor)) is not executor:
            raise PermissionError("Executor was not verified by this identity provider")
        self._verify_client(executor.credential_handle, executor.host,
                            executor.workspace_id, executor.principal_id)
        return executor.credential_handle

    def sql_client(self, executor):
        return self._client(executor).statement_execution

    def volume_client(self, executor):
        return self._client(executor).files

    def verify_run_as(self, execution_ref, expected_principal_id):
        if self._job_client is None:
            raise PermissionError("No explicit target-local Job identity reader")
        verify_job_run_as(self._job_client, execution_ref, expected_principal_id)


def verify_job_run_as(client, execution_ref: str, expected_principal_id: str) -> None:
    if not execution_ref.isdigit() or int(execution_ref) <= 0 or not expected_principal_id:
        raise ValueError("Explicit Job and expected principal required")
    job = client.jobs.get(job_id=int(execution_ref))
    settings = getattr(job, "settings", None)
    run_as = getattr(settings, "run_as", None)
    if (getattr(run_as, "service_principal_name", None) != expected_principal_id
            or getattr(run_as, "user_name", None)):
        raise PermissionError("Job run_as does not match expected service principal")


def verify_configured_job_run_as(environment, client_factory) -> None:
    execution_ref = environment.get("GSO_JOB_ID", "")
    if not execution_ref:
        return
    if not execution_ref.isdigit() or int(execution_ref) <= 0:
        raise ValueError("Invalid configured GSO Job ID")
    client = client_factory()
    principal = client.config.client_id or environment.get("DATABRICKS_CLIENT_ID", "")
    verify_job_run_as(client, execution_ref, principal)
