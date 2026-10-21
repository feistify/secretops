from secretops.vaults.base import (
    CreateOutcome,
    Vault,
    VaultConfigurationError,
    VaultError,
    VaultProviderError,
    VaultResponseError,
    VaultTimeoutError,
    VaultUnsupportedValueError,
    WriteOutcome,
)
from secretops.vaults.infisical_cli import InfisicalCliVault, InfisicalConnection


def get_vault(destination: dict | None = None) -> Vault:
    """Return the vault adapter used for secret operations.

    With a destination recorded in a plan, the adapter is bound to it for
    every call. Without one, the destination comes from configuration; that
    is only for commands that do not run saved plans.
    """
    if destination is None:
        return InfisicalCliVault()
    return InfisicalCliVault(connection=InfisicalConnection.from_destination(destination))


def configured_vault_destination() -> dict[str, str]:
    """Resolve the configured vault destination without a token or provider call."""
    return InfisicalCliVault().configured_connection().to_destination()


__all__ = [
    "CreateOutcome",
    "InfisicalCliVault",
    "Vault",
    "VaultConfigurationError",
    "VaultError",
    "VaultProviderError",
    "VaultResponseError",
    "VaultTimeoutError",
    "VaultUnsupportedValueError",
    "WriteOutcome",
    "configured_vault_destination",
    "get_vault",
]
