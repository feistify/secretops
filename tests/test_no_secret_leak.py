import json
import os
import subprocess


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

