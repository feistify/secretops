from abc import ABC, abstractmethod
from enum import Enum


class VaultError(Exception):
    """A vault call failed without a trustworthy answer.

    Messages are safe to print: they never contain secret values or provider
    output. Callers must stop the operation; an error never means "missing".
    """


class VaultConfigurationError(VaultError):
    """Required vault configuration or credentials are missing or invalid."""


class VaultProviderError(VaultError):
    """The provider CLI failed (authentication, permission, or availability)."""


class VaultTimeoutError(VaultError):
    """The provider CLI did not finish within the configured timeout."""


class VaultResponseError(VaultError):
    """The provider answer did not match the expected response protocol."""


class VaultUnsupportedValueError(VaultError):
    """The provider cannot store this value; nothing was sent to it."""


class WriteOutcome(Enum):
    """Result of a write that reached the provider.

    A write that fails before the provider CLI starts raises VaultError
    instead: nothing was sent. Once the CLI has started, only a read-back
    that returns the written value confirms the write; anything else is
    unknown, because the provider may still have stored the value.
    """

    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class CreateOutcome(Enum):
    """Result of a create-only request.

    CONFIRMED: the write was followed by a read-back that matched the value.
    EXISTS: the secret was present (or an atomic create reported a conflict);
    this call did not write. UNKNOWN: a write may have happened but was not
    confirmed. Failures before any write raise VaultError instead.
    """

    CONFIRMED = "confirmed"
    EXISTS = "exists"
    UNKNOWN = "unknown"


class Vault(ABC):
    # True only when create_secret refuses an existing secret atomically.
    # Declared by the adapter implementation; callers never set it.
    atomic_create = False

    def create_secret(self, name: str, env: str, value: str) -> CreateOutcome:
        """Create a secret that must not exist yet.

        This default checks presence and then writes, which is not atomic:
        another writer can create the secret between the check and the write,
        and the write then replaces it. Adapters that can refuse an existing
        secret atomically override this method and set `atomic_create`.
        """
        if self.secret_exists(name, env):
            return CreateOutcome.EXISTS
        outcome = self.write_secret(name, env, value)
        return CreateOutcome.CONFIRMED if outcome is WriteOutcome.CONFIRMED else CreateOutcome.UNKNOWN

    @abstractmethod
    def secret_exists(self, name: str, env: str) -> bool:
        """Return True when present, False when confirmed missing.

        An empty value counts as present. Raise VaultError when presence
        cannot be confirmed either way.
        """

    @abstractmethod
    def read_secret_value(self, name: str, env: str) -> str | None:
        """Return the stored value, or None when confirmed missing.

        An empty string is a present secret with an empty value. Raise
        VaultError when the value cannot be read. Never print or log it.
        """

    @abstractmethod
    def write_secret(self, name: str, env: str, value: str) -> WriteOutcome:
        """Store a secret value without printing, logging, or passing it as an arg.

        Raise VaultError if the write could not be sent. Never retry.
        """
