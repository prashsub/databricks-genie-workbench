"""Server-side identity verification without authority repair or credential fallback."""


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
