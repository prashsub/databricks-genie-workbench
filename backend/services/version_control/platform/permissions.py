"""Validate effective privileges, including inheritance, without widening grants."""

FACT_TABLES = frozenset({"genie_space_versions", "genie_space_registry", "genie_space_operations"})


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
