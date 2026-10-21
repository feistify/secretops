import os
import subprocess

import pytest

from secretops.policy import (
    get_action_policy,
    is_allowed,
    is_production_allowed,
    load_policy,
    require_allowed,
    require_environment_allowed,
    requires_approval,
)


DUMMY_SECRET_NAME = "DUMMY_SECRET"
DUMMY_SECRET_VALUE = "not-a-real-secret"


def run_secretops(*args: str, audit_path=None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env[DUMMY_SECRET_NAME] = DUMMY_SECRET_VALUE
    if audit_path is not None:
        env["SECRETOPS_AUDIT_PATH"] = str(audit_path)

    return subprocess.run(
        ["uv", "run", "secretops", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_allowed_policy_action_passes():
    require_allowed("vault.read.plan")

    assert is_allowed("vault.read.plan") is True


def test_denied_policy_action_fails():
    with pytest.raises(PermissionError):
        require_allowed("secret.reveal")

    assert is_allowed("secret.reveal") is False


def test_default_policy_contains_expected_actions():
    policy = load_policy()

    assert "vault.read.plan" in policy.allowed_actions
    assert "vault.read.check" in policy.allowed_actions
    assert "vault.secret.generate" in policy.allowed_actions
    assert "vault.secret.rotate" in policy.allowed_actions
    assert "vault.secret.import_file" in policy.allowed_actions
    assert "vault.secret.copy_many" in policy.allowed_actions
    assert "vault.secret.diff_envs" in policy.allowed_actions
    assert "github.env_secret.write" in policy.allowed_actions
    assert "github.env_secret.sync_many" in policy.allowed_actions
    assert "github.env_secret.verify_many" in policy.allowed_actions
    assert "secret.reveal" in policy.denied_actions
    assert "secret.export_all" in policy.denied_actions
    assert "secret.print_value" in policy.denied_actions
    assert "production.execute_without_approval" in policy.denied_actions


def test_default_policy_loads_action_metadata():
    read_policy = get_action_policy("vault.read.check")
    sync_policy = get_action_policy("github.env_secret.sync_many")
    verify_policy = get_action_policy("github.env_secret.verify_many")
    diff_policy = get_action_policy("vault.secret.diff_envs")

    assert read_policy.requires_approval is False
    assert read_policy.production_allowed is True
    assert "production" in read_policy.allowed_environments
    assert sync_policy.requires_approval is True
    assert sync_policy.production_allowed is False
    assert "dev" in sync_policy.allowed_environments
    assert "staging" in sync_policy.allowed_environments
    assert verify_policy.requires_approval is False
    assert verify_policy.production_allowed is True
    assert "production" in verify_policy.allowed_environments
    assert diff_policy.requires_approval is False
    assert diff_policy.production_allowed is True
    assert diff_policy.allowed_environments == ["dev", "staging", "production"]


def test_policy_approval_metadata_matches_current_actions():
    assert requires_approval("vault.read.check") is False
    for action in {
        "vault.secret.generate",
        "vault.secret.rotate",
        "vault.secret.copy_many",
        "vault.secret.import_file",
        "github.env_secret.write",
        "github.env_secret.sync_many",
    }:
        assert requires_approval(action) is True
    assert requires_approval("github.env_secret.verify_many") is False
    assert requires_approval("vault.secret.diff_envs") is False


def test_policy_denies_production_for_write_sync_generate_rotate_and_copy_actions():
    for action in {
        "vault.secret.generate",
        "vault.secret.rotate",
        "vault.secret.copy_many",
        "vault.secret.import_file",
        "github.env_secret.write",
        "github.env_secret.sync_many",
    }:
        assert is_production_allowed(action) is False
        with pytest.raises(PermissionError):
            require_environment_allowed(action, "production")

    require_environment_allowed("vault.read.check", "production")
    require_environment_allowed("github.env_secret.verify_many", "production")
    require_environment_allowed("vault.secret.diff_envs", "production")
    assert is_production_allowed("github.env_secret.verify_many") is True
    assert is_production_allowed("vault.secret.diff_envs") is True


def test_actions_command_lists_allowed_and_denied_actions_without_secret_values(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops("actions", audit_path=audit_path)

    output = combined_output(result)

    assert result.returncode == 0
    assert "Allowed actions" in output
    assert "Denied actions" in output
    assert "vault.read.plan" in output
    assert "vault.read.check" in output
    assert "vault.secret.generate" in output
    assert "vault.secret.rotate" in output
    assert "vault.secret.import_file" in output
    assert "vault.secret.copy_many" in output
    assert "vault.secret.diff_envs" in output
    assert "github.env_secret.write" in output
    assert "github.env_secret.sync_many" in output
    assert "secret.reveal" in output
    assert "secret.export_all" in output
    assert "secret.print_value" in output
    assert "production.execute_without_approval" in output
    assert DUMMY_SECRET_VALUE not in output
    if audit_path.exists():
        assert DUMMY_SECRET_VALUE not in audit_path.read_text()




def test_unknown_action_fails_closed_for_production_and_approval():
    assert is_allowed("unknown.action") is False
    assert is_production_allowed("unknown.action") is False
    assert requires_approval("unknown.action") is True
    with pytest.raises(PermissionError):
        require_allowed("unknown.action")
    with pytest.raises(PermissionError):
        require_environment_allowed("unknown.action", "production")


def test_policy_loads_from_package_default_when_cwd_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    policy = load_policy()

    assert "vault.read.check" in policy.allowed_actions


def test_policy_path_env_override(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "allowed_actions:\n"
        "  - custom.safe\n"
        "denied_actions:\n"
        "  - custom.denied\n"
        "actions:\n"
        "  custom.safe:\n"
        "    requires_approval: false\n"
        "    allowed_environments:\n"
        "      - dev\n"
        "    production_allowed: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SECRETOPS_POLICY_PATH", str(policy_path))

    policy = load_policy()

    assert policy.allowed_actions == ["custom.safe"]
    assert policy.denied_actions == ["custom.denied"]
    assert requires_approval("custom.safe", policy) is False
    assert is_production_allowed("custom.safe", policy) is False


def test_policy_empty_env_override_falls_back_to_package_default(monkeypatch):
    monkeypatch.setenv("SECRETOPS_POLICY_PATH", "")

    policy = load_policy()

    assert "vault.read.check" in policy.allowed_actions
