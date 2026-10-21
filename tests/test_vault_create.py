"""The create-only contract of vault adapters.

Every adapter returns CONFIRMED, EXISTS, or UNKNOWN from create_secret and
declares whether that refusal is atomic. Tests assert only the declared
guarantee: a non-atomic adapter can lose a race, and even an atomic create
can end UNKNOWN when the read-back does not match.
"""

import pytest

from secretops.vaults import (
    CreateOutcome,
    InfisicalCliVault,
    Vault,
    VaultProviderError,
    WriteOutcome,
)


class MemoryVault(Vault):
    """Check-then-write, using the default create_secret."""

    def __init__(self, stored=None, *, before_write=None, read_back=None, fail_check=False):
        self.stored = dict(stored or {})
        self.before_write = before_write
        self.read_back = read_back
        self.fail_check = fail_check
        self.writes = []

    def secret_exists(self, name, env):
        if self.fail_check:
            raise VaultProviderError("provider unavailable")
        return name in self.stored

    def read_secret_value(self, name, env):
        return self.stored.get(name)

    def write_secret(self, name, env, value):
        if self.before_write:
            self.before_write(self)
        self.writes.append(name)
        self.stored[name] = value
        stored = self.read_back if self.read_back is not None else self.stored[name]
        return WriteOutcome.CONFIRMED if stored == value else WriteOutcome.UNKNOWN


class AtomicMemoryVault(MemoryVault):
    """Conditional create, as a CAS-zero backend would provide."""

    atomic_create = True

    def create_secret(self, name, env, value):
        if self.before_write:
            self.before_write(self)
        if name in self.stored:
            return CreateOutcome.EXISTS
        self.stored[name] = value
        stored = self.read_back if self.read_back is not None else self.stored[name]
        return CreateOutcome.CONFIRMED if stored == value else CreateOutcome.UNKNOWN


def competing_writer(vault):
    vault.stored["NAME"] = "competitor"


@pytest.mark.parametrize("vault_class", [MemoryVault, AtomicMemoryVault])
def test_absent_secret_is_created_and_confirmed(vault_class):
    vault = vault_class()

    assert vault.create_secret("NAME", "dev", "value") is CreateOutcome.CONFIRMED
    assert vault.stored["NAME"] == "value"


@pytest.mark.parametrize("vault_class", [MemoryVault, AtomicMemoryVault])
def test_present_secret_is_not_written(vault_class):
    vault = vault_class({"NAME": "old"})

    assert vault.create_secret("NAME", "dev", "value") is CreateOutcome.EXISTS
    assert vault.stored["NAME"] == "old"


@pytest.mark.parametrize("vault_class", [MemoryVault, AtomicMemoryVault])
def test_mismatching_read_back_is_unknown(vault_class):
    vault = vault_class(read_back="something else")

    assert vault.create_secret("NAME", "dev", "value") is CreateOutcome.UNKNOWN


def test_non_atomic_create_loses_a_race_to_another_writer():
    # Documented limitation: the competitor's secret is replaced.
    vault = MemoryVault(before_write=competing_writer)

    assert vault.atomic_create is False
    assert vault.create_secret("NAME", "dev", "value") is CreateOutcome.CONFIRMED
    assert vault.stored["NAME"] == "value"


def test_atomic_create_refuses_a_racing_writer():
    vault = AtomicMemoryVault(before_write=competing_writer)

    assert vault.create_secret("NAME", "dev", "value") is CreateOutcome.EXISTS
    assert vault.stored["NAME"] == "competitor"


def test_failed_presence_check_raises_before_any_write():
    vault = MemoryVault(fail_check=True)

    with pytest.raises(VaultProviderError):
        vault.create_secret("NAME", "dev", "value")
    assert vault.writes == []


def test_infisical_cli_declares_non_atomic_create():
    assert InfisicalCliVault.atomic_create is False
    assert InfisicalCliVault.create_secret is Vault.create_secret
