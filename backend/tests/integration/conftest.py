"""M03-local registration; shared test configuration remains M08-owned."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: requires real Databricks/Delta platform"
    )
