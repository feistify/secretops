"""The GitHub CLI runs with an explicit environment, never the broker's own.

Contract: gh is found through a fixed PATH (or SECRETOPS_GH_BIN from broker
configuration) and receives only the stored-login location, TLS, proxy, and
locale variables, plus non-interactive flags set by the broker. GitHub
tokens, GH_HOST, GH_REPO, and unrelated broker credentials are never passed.
"""

import json
import sys

import pytest
from fake_process import FakeFactory, FakeProcess
from test_plan_binding import VERIFY_MANY_RECIPE, save_plan_from_recipe
from test_plans import (
    DUMMY_SECRET_NAME,
    DUMMY_SECRET_VALUE,
    github_plan,
    read_plan,
    run_secretops,
    write_plan,
)

from secretops.targets import github_cli
from secretops.targets.github_cli import (
    CHILD_PATH,
    GitHubCliEnvSecretTarget,
    GitHubConfigurationError,
    GitHubWriteOutcome,
    github_child_env,
    resolve_gh_binary,
)

GITHUB_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
UNRELATED_VARS = {
    "INFISICAL_TOKEN": "dummy-infisical-token",
    "AWS_SECRET_ACCESS_KEY": "dummy-aws-key",
    "SECRETOPS_INFISICAL_PROJECT_ID": "dummy-project",
    "GH_HOST": "evil.example.com",
    "GH_REPO": "evil-owner/evil-repo",
    "GH_DEBUG": "api",
    "LD_PRELOAD": "/tmp/evil.so",
    "PYTHONPATH": "/tmp/evil",
    "PATH": "/tmp/agent-writable",
    DUMMY_SECRET_NAME: DUMMY_SECRET_VALUE,
}
LOGIN_VARS = {
    "HOME": "/home/secretops-broker",
    "XDG_CONFIG_HOME": "/home/secretops-broker/.config",
    "GH_CONFIG_DIR": "/home/secretops-broker/.config/gh",
}
NETWORK_VARS = {
    "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    "HTTPS_PROXY": "http://proxy.example.com:3128",
    "NO_PROXY": "localhost",
    "LANG": "C.UTF-8",
}
FIXED = {
    "PATH": CHILD_PATH,
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
}
BROKER_ENVIRON = {
    **UNRELATED_VARS,
    **{name: f"dummy-{name.lower()}" for name in GITHUB_TOKEN_VARS},
    **LOGIN_VARS,
    **NETWORK_VARS,
}


def recording_target(environ, calls):
    # The factory records (command, kwargs) at launch, including env.
    factory = FakeFactory(FakeProcess(stdout="NAME\t2026-01-01\n"))
    factory.calls = calls
    return GitHubCliEnvSecretTarget(
        owner="example-owner",
        repo="secretops",
        environment="dev",
        popen_factory=factory,
        binary="/usr/bin/gh",
        environ=environ,
    )


def call_operation(target, operation):
    if operation == "write":
        assert target.set_secret(DUMMY_SECRET_NAME, DUMMY_SECRET_VALUE) is GitHubWriteOutcome.ACKNOWLEDGED
    else:
        assert target.list_secret_names() == ["NAME"]


# Exact environment, for secret writes and for metadata verification


@pytest.mark.parametrize("operation", ["write", "verify"])
def test_gh_receives_exactly_the_allowed_environment(operation):
    calls = []
    target = recording_target(BROKER_ENVIRON, calls)

    call_operation(target, operation)

    assert calls[0][1]["env"] == {**LOGIN_VARS, **NETWORK_VARS, **FIXED}


@pytest.mark.parametrize("operation", ["write", "verify"])
def test_gh_never_receives_tokens_or_unrelated_credentials(operation):
    calls = []
    target = recording_target(BROKER_ENVIRON, calls)

    call_operation(target, operation)

    env = calls[0][1]["env"]
    for name in (*GITHUB_TOKEN_VARS, "INFISICAL_TOKEN", "AWS_SECRET_ACCESS_KEY", "GH_HOST", "GH_REPO"):
        assert name not in env
    for value in BROKER_ENVIRON.values():
        if value not in {*LOGIN_VARS.values(), *NETWORK_VARS.values()}:
            assert value not in env.values()


def test_both_operations_use_the_same_environment():
    calls = []
    target = recording_target(BROKER_ENVIRON, calls)

    call_operation(target, "write")
    call_operation(target, "verify")

    assert calls[0][1]["env"] == calls[1][1]["env"] == github_child_env(BROKER_ENVIRON)


def test_stored_login_location_is_passed_unchanged():
    env = github_child_env(BROKER_ENVIRON)

    assert {name: env[name] for name in LOGIN_VARS} == LOGIN_VARS


def test_unset_login_location_is_not_invented():
    env = github_child_env({"HOME": "/home/secretops-broker", "INFISICAL_TOKEN": "dummy"})

    assert env == {"HOME": "/home/secretops-broker", **FIXED}


def test_caller_cannot_override_broker_flags():
    env = github_child_env({"GH_PROMPT_DISABLED": "0", "NO_COLOR": "", "PATH": "/tmp/evil"})

    assert env == FIXED


# Finding gh: fixed PATH or broker configuration, no fallback


def write_executable(path):
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_gh_is_never_looked_up_in_caller_path(tmp_path, monkeypatch):
    planted = tmp_path / "planted"
    planted.mkdir()
    write_executable(planted / "gh")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    write_executable(trusted / "gh")
    monkeypatch.setattr(github_cli, "CHILD_PATH", str(trusted))

    assert resolve_gh_binary({"PATH": str(planted)}) == str(trusted / "gh")


def test_missing_gh_is_a_clear_error_without_fallback(tmp_path, monkeypatch):
    planted = tmp_path / "planted"
    planted.mkdir()
    write_executable(planted / "gh")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(github_cli, "CHILD_PATH", str(empty))

    with pytest.raises(GitHubConfigurationError, match="gh CLI was not found.*SECRETOPS_GH_BIN"):
        resolve_gh_binary({"PATH": str(planted)})


def test_configured_gh_path_is_used(tmp_path):
    binary = write_executable(tmp_path / "gh")

    assert resolve_gh_binary({"SECRETOPS_GH_BIN": str(binary)}) == str(binary)


@pytest.mark.parametrize("kind", ["relative", "missing", "directory", "not_executable"])
def test_invalid_configured_gh_path_stops_before_gh(tmp_path, kind):
    if kind == "relative":
        configured = "bin/gh"
    elif kind == "missing":
        configured = str(tmp_path / "missing")
    elif kind == "directory":
        configured = str(tmp_path)
    else:
        target = tmp_path / "gh"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o600)
        configured = str(target)

    def run(command, **kwargs):
        raise AssertionError("gh must not be called")

    with pytest.raises(GitHubConfigurationError, match="SECRETOPS_GH_BIN"):
        GitHubCliEnvSecretTarget(
            owner="example-owner",
            repo="secretops",
            environment="dev",
            popen_factory=run,
            environ={"SECRETOPS_GH_BIN": configured},
        )


def test_gh_is_called_by_resolved_path():
    calls = []
    target = recording_target(BROKER_ENVIRON, calls)

    call_operation(target, "verify")

    assert calls[0][0][0] == "/usr/bin/gh"


# Through the CLI


def write_env_recording_gh(tmp_path):
    fake = tmp_path / "gh"
    record = tmp_path / "gh-env.json"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        f"open({str(record)!r}, 'w', encoding='utf-8').write(json.dumps(sorted(os.environ)))\n"
        "print('BACKUP_S3_BUCKET\\t2026-01-01T00:00:00Z')\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    return record


def write_marking_infisical(tmp_path):
    fake = tmp_path / "infisical"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        f"open({str(tmp_path / 'infisical-called')!r}, 'w').close()\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "child_env = os.environ.copy()\n"
        f"child_env[{DUMMY_SECRET_NAME!r}] = {DUMMY_SECRET_VALUE!r}\n"
        "raise SystemExit(subprocess.run(command, env=child_env).returncode)\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    return tmp_path


def assert_only_allowed_names_reached_gh(record):
    names = set(json.loads(record.read_text(encoding="utf-8")))
    assert names <= set(github_cli.PASSTHROUGH_VARS) | set(FIXED)
    assert {"PATH", "HOME", "GH_PROMPT_DISABLED"} <= names


@pytest.fixture
def broker_credentials(monkeypatch):
    # INFISICAL_TOKEN and DUMMY_SECRET are already in the test environment.
    for name in (*GITHUB_TOKEN_VARS, "GH_HOST", "GH_REPO"):
        monkeypatch.setenv(name, "dummy-value")


def test_broker_credentials_do_not_reach_gh_on_write(tmp_path, broker_credentials):
    plan_dir = tmp_path / "plans"
    record = write_env_recording_gh(tmp_path)
    fake_bin = write_marking_infisical(tmp_path)
    write_plan(plan_dir, github_plan("env-plan", status="approved", action="github.env_secret.write"))

    result = run_secretops(
        "execute-plan", "--id", "env-plan", audit_path=tmp_path / "audit.jsonl",
        plan_dir=plan_dir, path_prefix=fake_bin,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "infisical-called").exists()
    assert_only_allowed_names_reached_gh(record)


def test_broker_credentials_do_not_reach_gh_on_verification(tmp_path, broker_credentials):
    plan_dir = tmp_path / "plans"
    record = write_env_recording_gh(tmp_path)
    plan_id, _ = save_plan_from_recipe(tmp_path, plan_dir, VERIFY_MANY_RECIPE)

    result = run_secretops(
        "execute-plan", "--id", plan_id, audit_path=tmp_path / "audit.jsonl",
        plan_dir=plan_dir, path_prefix=tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert_only_allowed_names_reached_gh(record)


def test_missing_gh_fails_the_plan_before_any_secret_is_read(tmp_path, monkeypatch):
    not_executable = tmp_path / "gh-not-executable"
    not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    not_executable.chmod(0o600)
    monkeypatch.setenv("SECRETOPS_GH_BIN", str(not_executable))
    plan_dir = tmp_path / "plans"
    fake_bin = write_marking_infisical(tmp_path)
    plan_path = write_plan(
        plan_dir, github_plan("missing-gh", status="approved", action="github.env_secret.write")
    )

    result = run_secretops(
        "execute-plan", "--id", "missing-gh", audit_path=tmp_path / "audit.jsonl",
        plan_dir=plan_dir, path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "GitHub configuration error: SECRETOPS_GH_BIN must be an absolute path" in output
    assert "Traceback" not in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert DUMMY_SECRET_VALUE not in output
