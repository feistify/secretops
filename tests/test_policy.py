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


# Strict validation: unknown keys are an error naming the key and location

MARKER = "content-that-must-not-be-echoed"
VALID_ACTION = (
    "actions:\n"
    "  vault.read.check:\n"
    "    requires_approval: false\n"
    "    allowed_environments: [dev]\n"
    "    production_allowed: false\n"
)


def write_policy(tmp_path, text):
    path = tmp_path / "policy.yaml"
    path.write_text(f"allowed_actions: [{MARKER}]\ndenied_actions: []\n" + text, encoding="utf-8")
    return path


def test_packaged_default_policy_passes_strict_validation():
    from importlib import resources

    import yaml

    from secretops.policy import validate_policy_data

    text = resources.files("secretops.policies").joinpath("default.yaml").read_text(encoding="utf-8")
    validate_policy_data(yaml.safe_load(text), "default")


FIX_KEY = "correct or remove it"
FIX_VALUE = "correct its value"
FIX_LAYOUT = "correct its layout"
ENTRY = "actions entry 'vault.read.check'"


@pytest.mark.parametrize(
    "text,expected,fix",
    [
        (
            VALID_ACTION + "deny_actions: [x]\n",
            "unknown key 'deny_actions' in the top level "
            "(allowed: allowed_actions, denied_actions, actions)",
            FIX_KEY,
        ),
        (
            VALID_ACTION + "    requires_aproval: true\n",
            f"unknown key 'requires_aproval' in {ENTRY} "
            "(allowed: requires_approval, allowed_environments, production_allowed)",
            FIX_KEY,
        ),
        ("actions: [vault.read.check]\n", "actions must be a mapping of action names to settings", FIX_LAYOUT),
        ("actions:\n  vault.read.check: [dev]\n", f"{ENTRY} must be a mapping of keys to values", FIX_LAYOUT),
        (
            "actions:\n  vault.read.check:\n    production_allowed: \"false\"\n",
            f"production_allowed in {ENTRY} must be true or false",
            FIX_VALUE,
        ),
        (
            "actions:\n  vault.read.check:\n    requires_approval: 0\n",
            f"requires_approval in {ENTRY} must be true or false",
            FIX_VALUE,
        ),
        (
            "actions:\n  vault.read.check:\n    allowed_environments: dev\n",
            f"allowed_environments in {ENTRY} must be a list of names",
            FIX_VALUE,
        ),
        (
            "actions:\n  vault.read.check:\n    allowed_environments: [dev, 1]\n",
            f"allowed_environments in {ENTRY} must be a list of names",
            FIX_VALUE,
        ),
    ],
    ids=[
        "top_level", "nested", "actions_list", "entry_list",
        "quoted_boolean", "number_boolean", "string_list", "non_string_item",
    ],
)
def test_unknown_or_mistyped_policy_keys_are_refused(tmp_path, text, expected, fix):
    from secretops.policy import PolicyError

    path = write_policy(tmp_path, text)

    with pytest.raises(PolicyError) as raised:
        load_policy(path)

    message = str(raised.value)
    assert message == f"policy file {path}: {expected}; {fix}; see docs/CONFIGURATION.md#policy-file"
    assert MARKER not in message


@pytest.mark.parametrize(
    "text,key",
    [
        ("allowed_actions: vault.read.check\n", "allowed_actions"),
        ("denied_actions: secret.reveal\n", "denied_actions"),
        ("allowed_actions:\n", "allowed_actions"),
    ],
    ids=["allowed_string", "denied_string", "allowed_null"],
)
def test_top_level_action_lists_must_be_lists_of_names(tmp_path, text, key):
    from secretops.policy import PolicyError

    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(PolicyError, match=f": {key} in the top level must be a list of names; "):
        load_policy(path)


@pytest.mark.parametrize("kind", ["missing", "unreadable"])
def test_missing_or_unreadable_policy_file_is_a_clean_error(tmp_path, kind):
    from secretops.policy import PolicyError

    path = tmp_path / "policy.yaml"
    if kind == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root ignores file permissions")
        path.write_text(f"allowed_actions: [{MARKER}]\n", encoding="utf-8")
        path.chmod(0)

    with pytest.raises(PolicyError) as raised:
        load_policy(path)

    assert str(raised.value) == (
        f"policy file {path} is missing or not readable by this account; "
        "see docs/CONFIGURATION.md#policy-file"
    )


def test_empty_action_entry_and_valid_custom_policy_still_load(tmp_path):
    policy = load_policy(write_policy(tmp_path, VALID_ACTION + "  custom.empty:\n"))

    assert policy.allowed_actions == [MARKER]
    assert get_action_policy("custom.empty", policy).requires_approval is True
    assert get_action_policy("vault.read.check", policy).allowed_environments == ["dev"]


def test_invalid_policy_yaml_names_the_line_only(tmp_path):
    from secretops.policy import PolicyError

    path = tmp_path / "policy.yaml"
    path.write_text(f"allowed_actions: [ok]\nactions: {{{MARKER}: [\n", encoding="utf-8")

    with pytest.raises(PolicyError) as raised:
        load_policy(path)

    assert str(raised.value).startswith(f"policy file {path} is not valid YAML at line ")
    assert MARKER not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_every_command_stops_on_an_invalid_policy(tmp_path, monkeypatch):
    path = write_policy(tmp_path, VALID_ACTION + "    production_alowed: true\n")
    monkeypatch.setenv("SECRETOPS_POLICY_PATH", str(path))

    for command in (["version"], ["actions"], ["show-plan", "--id", "missing"]):
        result = run_secretops(*command, audit_path=tmp_path / "audit.jsonl")

        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr.strip() == (
            f"FAIL policy file {path}: unknown key 'production_alowed' in actions entry "
            "'vault.read.check' (allowed: requires_approval, allowed_environments, "
            "production_allowed); correct or remove it; see docs/CONFIGURATION.md#policy-file"
        )
        assert MARKER not in combined_output(result)
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize(
    "text,key,line",
    [
        ("allowed_actions: [a]\ndenied_actions: []\nallowed_actions: [b]\n", "allowed_actions", 3),
        (VALID_ACTION + "    production_allowed: true\n", "production_allowed", 8),
        (VALID_ACTION + "  vault.read.check:\n    requires_approval: true\n", "vault.read.check", 8),
    ],
    ids=["top_level", "nested", "action_entry"],
)
def test_duplicate_policy_keys_are_refused(tmp_path, text, key, line):
    from secretops.policy import PolicyError

    path = tmp_path / "policy.yaml"
    if text.startswith("actions:"):
        path.write_text(f"allowed_actions: [{MARKER}]\ndenied_actions: []\n" + text, encoding="utf-8")
    else:
        path.write_text(text, encoding="utf-8")

    with pytest.raises(PolicyError) as raised:
        load_policy(path)

    assert str(raised.value) == (
        f"policy file {path}: duplicate key '{key}' at line {line}; keep only one of them; "
        "see docs/CONFIGURATION.md#policy-file"
    )
    assert MARKER not in str(raised.value)
