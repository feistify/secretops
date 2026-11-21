import os
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


@pytest.fixture
def ca_dir(tmp_path, monkeypatch):
    """A directory the CA file check trusts: the test's own account may own
    it, and the check stops there instead of walking up through /tmp."""
    from secretops.vaults import infisical_cli

    monkeypatch.setattr(infisical_cli, "CA_TRUSTED_OWNERS", frozenset({0, os.getuid()}))
    monkeypatch.setattr(infisical_cli, "CA_CHECK_TOP", str(tmp_path))
    tmp_path.chmod(0o755)
    return tmp_path


def write_ca(directory, name="ca.pem", text=None, mode=0o644):
    path = directory / name
    path.write_text(text if text is not None else
                    "-----BEGIN CERTIFICATE-----\nnot-a-real-certificate\n-----END CERTIFICATE-----\n")
    path.chmod(mode)
    return path
