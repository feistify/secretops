import pytest

# Dummy connection settings for the fake Infisical CLI. The token is not a
# real credential; tests that need it missing remove it with monkeypatch.
TEST_VAULT_ENV = {
    "INFISICAL_TOKEN": "dummy-machine-token",
    "SECRETOPS_INFISICAL_PROJECT_ID": "dummy-project",
    "SECRETOPS_INFISICAL_PATH": "/",
}


@pytest.fixture(autouse=True)
def vault_connection_env(monkeypatch):
    for name, value in TEST_VAULT_ENV.items():
        monkeypatch.setenv(name, value)
