"""Validate effective privileges, including inheritance, without widening grants."""

FACT_TABLES = frozenset({"genie_space_versions", "genie_space_registry", "genie_space_operations"})


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
