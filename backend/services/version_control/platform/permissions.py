"""Validate effective privileges, including inheritance, without widening grants."""

FACT_TABLES = frozenset({"genie_space_versions", "genie_space_registry", "genie_space_operations"})
ARTIFACT_VOLUMES = frozenset({"vc_snapshots", "vc_approval_evidence", "vc_outbound_packages", "vc_target_receipts"})


def verify_artifact_permissions(volumes, expected_writers, operation_writers, source_principals) -> bool:
    if set(volumes) != ARTIFACT_VOLUMES or set(expected_writers) != ARTIFACT_VOLUMES:
        return False
    if not operation_writers or set(operation_writers) & set(source_principals):
        return False
    if len(set(expected_writers.values())) != len(ARTIFACT_VOLUMES) or not all(expected_writers.values()):
        return False
    securables = set()
    runtime_principals = set(expected_writers.values()) | set(source_principals) | set(operation_writers)
    for name, volume in volumes.items():
        securable = volume.get("securable", "")
        if (len(securable.split(".")) != 3 or not securable.endswith(f".{name}")
                or "/" in securable or volume.get("privileges_verified") is not True
                or not volume.get("owner") or volume["owner"] in runtime_principals
                or set(volume.get("writers", ())) != {expected_writers[name]}):
            return False
        if name != "vc_outbound_packages" and expected_writers[name] in source_principals:
            return False
        securables.add(securable)
    return len(securables) == len(ARTIFACT_VOLUMES)


def verify_coordination_permissions(proof) -> bool:
    principals = [proof.get(role) for role in ("owner", "executor", "enrollment")]
    return (
        all(principals) and len(set(principals)) == 3
        and set(proof.get("executor_privileges", ())) == {"SELECT", "UPDATE"}
        and set(proof.get("enrollment_privileges", ())) == {"SELECT", "INSERT"}
        and type(proof.get("max_concurrent_runs")) is int
        and proof["max_concurrent_runs"] == 1
        and proof.get("queue_enabled") is True
        and proof.get("isolation") == "Serializable"
    )


def verify_fact_permissions(facts) -> bool:
    if set(facts) != FACT_TABLES:
        return False
    return all(
        fact.get("append_only") is True
        and bool(fact.get("owner")) and bool(fact.get("principal"))
        and fact["owner"] != fact["principal"]
        and set(fact.get("privileges", ())) == {"SELECT", "INSERT"}
        for fact in facts.values()
    )
