import json
import os
import subprocess

import pytest


DUMMY_SECRET_NAME = "DUMMY_SECRET"
DUMMY_SECRET_VALUE = "not-a-real-secret"


def run_secretops(
    *args: str,
    audit_path=None,
    path_prefix=None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env[DUMMY_SECRET_NAME] = DUMMY_SECRET_VALUE
    if audit_path is not None:
        env["SECRETOPS_AUDIT_PATH"] = str(audit_path)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
        fake_infisical = os.path.join(str(path_prefix), "infisical")
        if os.path.exists(fake_infisical):
            env["SECRETOPS_INFISICAL_BIN"] = fake_infisical
        fake_gh = os.path.join(str(path_prefix), "gh")
        if os.path.exists(fake_gh):
            env["SECRETOPS_GH_BIN"] = fake_gh

    return subprocess.run(
        ["uv", "run", "secretops", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def fake_infisical_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "infisical"
    fake.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys

args = sys.argv[1:]
if "secrets" in args:
    sys.exit(99)
try:
    separator = args.index("--")
except ValueError:
    sys.exit(2)
command = args[separator + 1:]
provider_env = os.environ.copy()
provider_env["DUMMY_SECRET"] = "not-a-real-secret"
result = subprocess.run(command, env=provider_env)
sys.exit(result.returncode)
""",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    return bin_dir


def combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def assert_no_secret_leak(result: subprocess.CompletedProcess[str], audit_path=None) -> None:
    assert DUMMY_SECRET_VALUE not in result.stdout
    assert DUMMY_SECRET_VALUE not in result.stderr
    if audit_path is not None and audit_path.exists():
        assert DUMMY_SECRET_VALUE not in audit_path.read_text()


def read_audit_events(audit_path):
    return [json.loads(line) for line in audit_path.read_text().splitlines()]


def test_plan_vault_read_prints_secret_name_not_secret_value():
    result = run_secretops(
        "plan-vault-read",
        "--name",
        DUMMY_SECRET_NAME,
        "--env",
        "dev",
    )

    output = combined_output(result)

    assert result.returncode == 0
    assert DUMMY_SECRET_NAME in output
    assert_no_secret_leak(result)


def test_check_vault_read_prints_secret_name_not_secret_value(tmp_path):
    fake_bin = fake_infisical_bin(tmp_path)

    result = run_secretops(
        "check-vault-read",
        "--name",
        DUMMY_SECRET_NAME,
        "--env",
        "dev",
        path_prefix=fake_bin,
    )

    output = combined_output(result)

    assert DUMMY_SECRET_NAME in output
    assert_no_secret_leak(result)


def test_plan_vault_read_writes_safe_audit_event(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops(
        "plan-vault-read",
        "--name",
        DUMMY_SECRET_NAME,
        "--env",
        "dev",
        audit_path=audit_path,
    )

    events = read_audit_events(audit_path)

    assert result.returncode == 0
    assert audit_path.exists()
    assert len(events) == 1
    assert events[0]["action"] == "plan-vault-read"
    assert events[0]["secret_name"] == DUMMY_SECRET_NAME
    assert events[0]["env"] == "dev"
    assert events[0]["status"] == "planned"
    assert events[0]["secret_value_logged"] is False
    assert "timestamp" in events[0]
    assert_no_secret_leak(result, audit_path)


def test_check_vault_read_writes_safe_audit_event(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"
    fake_bin = fake_infisical_bin(tmp_path)

    result = run_secretops(
        "check-vault-read",
        "--name",
        DUMMY_SECRET_NAME,
        "--env",
        "dev",
        audit_path=audit_path,
        path_prefix=fake_bin,
    )

    events = read_audit_events(audit_path)

    assert audit_path.exists()
    assert len(events) == 1
    assert events[0]["action"] == "check-vault-read"
    assert events[0]["secret_name"] == DUMMY_SECRET_NAME
    assert events[0]["env"] == "dev"
    assert events[0]["status"] in {"ok", "fail"}
    assert events[0]["secret_value_logged"] is False
    assert "timestamp" in events[0]
    assert_no_secret_leak(result, audit_path)


# check-vault-read applies the environment policy of a vault.read.check plan


def recording_infisical_bin(tmp_path):
    bin_dir = fake_infisical_bin(tmp_path)
    fake = bin_dir / "infisical"
    marker = tmp_path / "infisical-called"
    fake.write_text(
        fake.read_text(encoding="utf-8").replace(
            "args = sys.argv[1:]\n",
            f"open({str(marker)!r}, 'a').close()\nargs = sys.argv[1:]\n",
        ),
        encoding="utf-8",
    )
    return bin_dir, marker


def test_check_vault_read_refuses_an_environment_the_policy_denies(tmp_path, monkeypatch):
    # The default policy allows the check in dev, staging and production only.
    from secretops.policy import require_environment_allowed

    audit_path = tmp_path / "audit.jsonl"
    bin_dir, marker = recording_infisical_bin(tmp_path)
    for env in ("qa", "prod"):
        with pytest.raises(PermissionError):
            require_environment_allowed("vault.read.check", env)

        result = run_secretops(
            "check-vault-read", "--name", DUMMY_SECRET_NAME, "--env", env,
            audit_path=audit_path, path_prefix=bin_dir,
        )

        assert result.returncode == 1
        assert result.stdout.strip().splitlines() == [
            f"FAIL environment denied by policy for vault.read.check: {env}",
            "no provider call was made",
        ]
        assert "Traceback" not in combined_output(result)
    assert not marker.exists()
    events = read_audit_events(audit_path)
    assert [(event["env"], event["status"], event["failure"]) for event in events] == [
        ("qa", "fail", "policy"),
        ("prod", "fail", "policy"),
    ]
    assert_no_secret_leak(result, audit_path)


def test_check_vault_read_allows_production_as_the_default_policy_does(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    bin_dir, marker = recording_infisical_bin(tmp_path)

    result = run_secretops(
        "check-vault-read", "--name", DUMMY_SECRET_NAME, "--env", "production",
        audit_path=audit_path, path_prefix=bin_dir,
    )

    assert result.returncode == 0, combined_output(result)
    assert f"OK secret exists: {DUMMY_SECRET_NAME}" in result.stdout
    assert marker.exists()
    assert_no_secret_leak(result, audit_path)


def test_check_vault_read_follows_a_custom_environment_policy(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "allowed_actions: [vault.read.check]\n"
        "denied_actions: []\n"
        "actions:\n"
        "  vault.read.check:\n"
        "    requires_approval: false\n"
        "    allowed_environments: [dev]\n"
        "    production_allowed: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SECRETOPS_POLICY_PATH", str(policy_path))
    bin_dir, marker = recording_infisical_bin(tmp_path)

    audit_path = tmp_path / "audit.jsonl"
    allowed = run_secretops(
        "check-vault-read", "--name", DUMMY_SECRET_NAME, "--env", "dev",
        audit_path=audit_path, path_prefix=bin_dir,
    )
    assert allowed.returncode == 0, combined_output(allowed)
    assert marker.exists()
    marker.unlink()
    denied = {
        env: run_secretops(
            "check-vault-read", "--name", DUMMY_SECRET_NAME, "--env", env,
            audit_path=audit_path, path_prefix=bin_dir,
        )
        for env in ("staging", "production")
    }

    assert denied["staging"].stdout.splitlines()[0] == (
        "FAIL environment denied by policy for vault.read.check: staging"
    )
    assert denied["production"].stdout.splitlines()[0] == (
        "FAIL production execution denied by policy: vault.read.check"
    )
    assert all(result.returncode == 1 for result in denied.values())
    assert not marker.exists()



def test_check_vault_read_refuses_a_denied_action(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("allowed_actions: []\ndenied_actions: [vault.read.check]\n", encoding="utf-8")
    monkeypatch.setenv("SECRETOPS_POLICY_PATH", str(policy_path))
    bin_dir, marker = recording_infisical_bin(tmp_path)

    result = run_secretops(
        "check-vault-read", "--name", DUMMY_SECRET_NAME, "--env", "dev",
        audit_path=tmp_path / "audit.jsonl", path_prefix=bin_dir,
    )

    assert result.returncode == 1
    assert result.stdout.strip().splitlines() == [
        "FAIL Action denied by policy: vault.read.check",
        "no provider call was made",
    ]
    assert "Traceback" not in combined_output(result)
    assert not marker.exists()


def test_policy_refusal_with_an_unwritable_audit_log_does_not_traceback(tmp_path):
    audit_path = tmp_path / "audit-is-a-directory"
    audit_path.mkdir()
    bin_dir, marker = recording_infisical_bin(tmp_path)

    result = run_secretops(
        "check-vault-read", "--name", DUMMY_SECRET_NAME, "--env", "qa",
        audit_path=audit_path, path_prefix=bin_dir,
    )

    assert result.returncode == 1
    assert result.stdout.strip().splitlines() == [
        "WARNING the audit record could not be written",
        "FAIL environment denied by policy for vault.read.check: qa",
        "no provider call was made",
    ]
    assert "Traceback" not in combined_output(result)
    assert not marker.exists()


@pytest.mark.parametrize("command", ["execute-recipe", "execute"])
def test_vault_read_check_recipe_applies_the_environment_policy(tmp_path, command):
    audit_path = tmp_path / "audit.jsonl"
    bin_dir, marker = recording_infisical_bin(tmp_path)
    recipe = tmp_path / "check.yaml"
    recipe.write_text(
        f"action: vault.read.check\nsecret_name: {DUMMY_SECRET_NAME}\nenv: qa\n", encoding="utf-8"
    )

    refused = run_secretops(command, "--file", str(recipe), audit_path=audit_path, path_prefix=bin_dir)

    assert refused.returncode == 1
    assert refused.stdout.strip().splitlines() == [
        "FAIL environment denied by policy for vault.read.check: qa",
        "no provider call was made",
    ]
    assert not marker.exists()
    event = read_audit_events(audit_path)[-1]
    assert (event["action"], event["env"], event["status"], event["failure"]) == (
        "vault.read.check", "qa", "failure", "policy",
    )

    recipe.write_text(
        f"action: vault.read.check\nsecret_name: {DUMMY_SECRET_NAME}\nenv: production\n",
        encoding="utf-8",
    )
    allowed = run_secretops(command, "--file", str(recipe), audit_path=audit_path, path_prefix=bin_dir)

    assert allowed.returncode == 0, combined_output(allowed)
    assert marker.exists()
    assert_no_secret_leak(allowed, audit_path)
