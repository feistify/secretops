import json
import os
import re
import subprocess

import pytest

from secretops.vaults import (
    Vault,
    VaultConfigurationError,
    VaultProviderError,
    VaultResponseError,
    VaultTimeoutError,
    VaultUnsupportedValueError,
    WriteOutcome,
    get_vault,
)
from secretops.vaults.infisical_cli import InfisicalCliVault

from fake_infisical import SetRejected, parse_set_args


DUMMY_SECRET_NAME = "DUMMY_SECRET"
DUMMY_SECRET_VALUE = "not-a-real-secret"
TEST_ENVIRON = {
    "INFISICAL_TOKEN": "dummy-machine-token",
    "SECRETOPS_INFISICAL_PROJECT_ID": "dummy-project",
    "SECRETOPS_INFISICAL_PATH": "/",
}
ALLOWED_CHILD_ENV = {"PATH", "HOME", "INFISICAL_TOKEN", "SSL_CERT_FILE", "SSL_CERT_DIR"}


def make_vault(runner, **environ_overrides):
    environ = {**TEST_ENVIRON, **environ_overrides}
    environ = {key: value for key, value in environ.items() if value is not None}
    return InfisicalCliVault(runner=runner, binary="infisical", environ=environ)


def fake_cli(vault_secrets=None, calls=None):
    """Runner that behaves like `infisical run`: the child gets the CLI's own
    environment plus the vault secrets, and the real helper code runs."""

    def run(command, **kwargs):
        if calls is not None:
            calls.append((command, kwargs))
        if "run" not in command:
            if "set" in command:
                try:
                    stored, _ = parse_set_args(command[command.index("set") + 1 :])
                except SetRejected:
                    return subprocess.CompletedProcess(command, 1, "", "provider error")
                if vault_secrets is not None:
                    vault_secrets.update(stored)
            return subprocess.CompletedProcess(command, 0, "", "")
        child_env = dict(kwargs["env"])
        child_env.update(vault_secrets or {})
        child = command[command.index("--") + 1 :]
        return subprocess.run(child, text=True, capture_output=True, env=child_env, check=False)

    return run


def canned(stdout="", returncode=0):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout, "provider error")

    return run


def nonce_of(command):
    return command[command.index("-c") + 2]


def test_existing_secret_is_present():
    vault = make_vault(fake_cli({DUMMY_SECRET_NAME: DUMMY_SECRET_VALUE}))

    assert vault.secret_exists(DUMMY_SECRET_NAME, "dev") is True


def test_missing_secret_is_missing():
    vault = make_vault(fake_cli({}))

    assert vault.secret_exists(DUMMY_SECRET_NAME, "dev") is False
    assert vault.read_secret_value(DUMMY_SECRET_NAME, "dev") is None


def test_empty_value_is_present_and_distinct_from_missing():
    vault = make_vault(fake_cli({DUMMY_SECRET_NAME: ""}))

    assert vault.secret_exists(DUMMY_SECRET_NAME, "dev") is True
    assert vault.read_secret_value(DUMMY_SECRET_NAME, "dev") == ""


def test_read_returns_stored_value():
    vault = make_vault(fake_cli({DUMMY_SECRET_NAME: DUMMY_SECRET_VALUE}))

    assert vault.read_secret_value(DUMMY_SECRET_NAME, "dev") == DUMMY_SECRET_VALUE


def test_broker_variable_does_not_leak_into_vault_answers(monkeypatch):
    monkeypatch.setenv(DUMMY_SECRET_NAME, "local-broker-value")
    vault = InfisicalCliVault(runner=fake_cli({}), binary="infisical", environ={**os.environ})

    assert vault.secret_exists(DUMMY_SECRET_NAME, "dev") is False
    assert vault.read_secret_value(DUMMY_SECRET_NAME, "dev") is None


def test_child_environment_is_allowlisted_with_fresh_home():
    calls = []
    seen = {}

    def run(command, **kwargs):
        calls.append((command, kwargs))
        home = kwargs["env"]["HOME"]
        seen["home"] = home
        seen["home_exists"] = os.path.isdir(home)
        seen["home_empty"] = os.listdir(home) == []
        seen["home_mode"] = oct(os.stat(home).st_mode & 0o777)
        return fake_cli({})(command, **kwargs)

    vault = make_vault(run, DBUS_SESSION_BUS_ADDRESS="unix:path=/dummy", XDG_CONFIG_HOME="/dummy")
    vault.secret_exists(DUMMY_SECRET_NAME, "dev")

    env = calls[0][1]["env"]
    assert set(env) <= ALLOWED_CHILD_ENV
    assert env["INFISICAL_TOKEN"] == TEST_ENVIRON["INFISICAL_TOKEN"]
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert seen["home_exists"] and seen["home_empty"]
    assert seen["home_mode"] == "0o700"
    assert not os.path.exists(seen["home"])


def test_tls_trust_settings_pass_through_only_when_set():
    calls = []
    vault = make_vault(fake_cli({}, calls), SSL_CERT_FILE="/etc/ssl/internal-ca.pem")
    vault.secret_exists(DUMMY_SECRET_NAME, "dev")

    env = calls[0][1]["env"]
    assert env["SSL_CERT_FILE"] == "/etc/ssl/internal-ca.pem"
    assert "SSL_CERT_DIR" not in env


def test_read_flags_are_explicit():
    calls = []
    vault = make_vault(fake_cli({}, calls))
    vault.secret_exists(DUMMY_SECRET_NAME, "dev")

    command = calls[0][0]
    cli_args = command[: command.index("--")]
    assert cli_args[:4] == ["infisical", "--domain", "https://eu.infisical.com", "run"]
    for flag in [
        "--env=dev",
        "--projectId=dummy-project",
        "--path=/",
        "--expand=false",
        "--secret-overriding=false",
        "--include-imports=false",
    ]:
        assert flag in cli_args
    assert not any(arg.startswith("--token") for arg in command)
    assert TEST_ENVIRON["INFISICAL_TOKEN"] not in command


def test_helper_runs_isolated_with_name_as_argv():
    calls = []
    vault = make_vault(fake_cli({}, calls))
    vault.secret_exists(DUMMY_SECRET_NAME, "dev")

    command = calls[0][0]
    child = command[command.index("--") + 1 :]
    assert os.path.isabs(child[0])
    assert child[1:3] == ["-I", "-c"]
    assert DUMMY_SECRET_NAME not in child[3]
    assert child[-1] == DUMMY_SECRET_NAME


def file_arg_path(command):
    return next(arg for arg in command if arg.startswith("--file=")).split("=", 1)[1]


def test_write_flags_are_explicit_and_value_uses_temp_file():
    calls = []
    seen = {}

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "set" in command:
            temp_path = file_arg_path(command)
            seen["temp_path"] = temp_path
            seen["temp_exists_during_call"] = os.path.exists(temp_path)
            seen["temp_mode"] = os.stat(temp_path).st_mode & 0o777
            with open(temp_path, encoding="utf-8") as handle:
                seen["temp_content"] = handle.read()
        return fake_cli(store)(command, **kwargs)

    store = {}
    vault = make_vault(run)

    assert vault.write_secret(DUMMY_SECRET_NAME, "dev", DUMMY_SECRET_VALUE) is WriteOutcome.CONFIRMED

    command = calls[0][0]
    assert command[:5] == ["infisical", "--domain", "https://eu.infisical.com", "secrets", "set"]
    for flag in ["--projectId=dummy-project", "--path=/", "--type=shared"]:
        assert flag in command
    assert command[command.index("--env") + 1] == "dev"
    assert not any(arg.startswith("--expand") for arg in command)
    assert not any(arg.startswith("--token") for arg in command)
    # The machine-token CLI stores `NAME=@path` literally, so no NAME= argument.
    assert not any(arg.startswith(f"{DUMMY_SECRET_NAME}=") for arg in command)
    assert not any(DUMMY_SECRET_VALUE in arg for arg in command)
    assert set(calls[0][1]["env"]) <= ALLOWED_CHILD_ENV
    assert seen["temp_path"].endswith(".yaml")
    assert seen["temp_exists_during_call"] is True
    assert seen["temp_mode"] == 0o600
    assert seen["temp_content"] == f'"{DUMMY_SECRET_NAME}": "{DUMMY_SECRET_VALUE}"\n'
    assert store == {DUMMY_SECRET_NAME: DUMMY_SECRET_VALUE}
    assert not os.path.exists(seen["temp_path"])


TRICKY_VALUES = [
    "0o17",
    "true",
    "null",
    "123",
    "~",
    "  leading and trailing spaces  ",
    'quotes "double" and \'single\'',
    "key=value=more",
    "${OTHER_SECRET} and ${env.OTHER}",
    "@/etc/passwd",
    "# not a comment",
    "unicode čćžšđ 🙂",
    "line one\nline two\n",
    "trailing newline\n",
    "tab\tand\rcarriage",
    "back\\slash",
    "x" * 500,
]


@pytest.mark.parametrize("value", TRICKY_VALUES)
def test_write_round_trips_value_through_double_quoted_yaml(value):
    store = {}
    contents = []

    def run(command, **kwargs):
        if "set" in command:
            with open(file_arg_path(command), encoding="utf-8") as handle:
                contents.append(handle.read())
        return fake_cli(store)(command, **kwargs)

    vault = make_vault(run)

    assert vault.write_secret(DUMMY_SECRET_NAME, "dev", value) is WriteOutcome.CONFIRMED
    assert store == {DUMMY_SECRET_NAME: value}
    # The CLI parses YAML 1.2, where plain scalars like 0o17, true, or null
    # are not strings. One line, key and value both double-quoted, keeps
    # every value a string for any YAML parser.
    [content] = contents
    assert re.fullmatch(r'"[A-Z_]+": "(?:[^"\\\n]|\\.)*"\n', content), content


@pytest.mark.parametrize("value", ["", " ", "\n", " \t\n "])
def test_empty_or_whitespace_value_is_refused_before_cli_start(value):
    calls = []
    vault = make_vault(fake_cli({}, calls))

    with pytest.raises(VaultUnsupportedValueError, match="empty or whitespace-only"):
        vault.write_secret(DUMMY_SECRET_NAME, "dev", value)
    assert calls == []


def test_existing_empty_secret_is_still_present_and_reads_empty():
    vault = make_vault(fake_cli({DUMMY_SECRET_NAME: ""}))

    assert vault.secret_exists(DUMMY_SECRET_NAME, "dev") is True
    assert vault.read_secret_value(DUMMY_SECRET_NAME, "dev") == ""


def test_fake_cli_stores_at_path_literally_like_the_token_cli():
    # Regression guard for the fake itself: the NAS check found that the
    # machine-token CLI does not expand NAME=@path.
    assert parse_set_args([f"{DUMMY_SECRET_NAME}=@/tmp/file", "--env", "dev"]) == (
        {DUMMY_SECRET_NAME: "@/tmp/file"},
        None,
    )


def test_write_removes_temp_file_when_cli_fails():
    seen = {}

    def run(command, **kwargs):
        if "set" in command:
            seen["temp_path"] = file_arg_path(command)
            return subprocess.CompletedProcess(command, 1, "", "provider error")
        return fake_cli({})(command, **kwargs)

    vault = make_vault(run)

    assert vault.write_secret(DUMMY_SECRET_NAME, "dev", DUMMY_SECRET_VALUE) is WriteOutcome.UNKNOWN
    assert not os.path.exists(seen["temp_path"])


def test_read_value_is_not_in_command_args():
    calls = []
    vault = make_vault(fake_cli({DUMMY_SECRET_NAME: DUMMY_SECRET_VALUE}, calls))

    assert vault.read_secret_value(DUMMY_SECRET_NAME, "dev") == DUMMY_SECRET_VALUE
    assert DUMMY_SECRET_VALUE not in calls[0][0]


@pytest.mark.parametrize(
    "missing",
    ["INFISICAL_TOKEN", "SECRETOPS_INFISICAL_PROJECT_ID", "SECRETOPS_INFISICAL_PATH"],
)
def test_missing_configuration_stops_before_cli(missing):
    calls = []
    vault = make_vault(fake_cli({}, calls), **{missing: None})

    with pytest.raises(VaultConfigurationError):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")
    with pytest.raises(VaultConfigurationError):
        vault.write_secret(DUMMY_SECRET_NAME, "dev", DUMMY_SECRET_VALUE)
    assert calls == []


def test_relative_path_is_rejected():
    calls = []
    vault = make_vault(fake_cli({}, calls), SECRETOPS_INFISICAL_PATH="apps")

    with pytest.raises(VaultConfigurationError):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")
    assert calls == []


def test_creating_adapter_needs_no_configuration():
    vault = InfisicalCliVault(runner=fake_cli({}), binary="infisical", environ={})

    assert isinstance(vault, Vault)


def test_cli_failure_is_an_error_not_missing():
    vault = make_vault(canned(returncode=1))

    with pytest.raises(VaultProviderError):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")
    with pytest.raises(VaultProviderError):
        vault.read_secret_value(DUMMY_SECRET_NAME, "dev")


@pytest.mark.parametrize(
    "make_stdout",
    [
        lambda nonce: "",
        lambda nonce: "absent\n",
        lambda nonce: json.dumps({"nonce": nonce, "present": False}),
        lambda nonce: json.dumps({"nonce": nonce, "present": False}) + "\nextra\n",
        lambda nonce: "warning\n" + json.dumps({"nonce": nonce, "present": False}) + "\n",
        lambda nonce: json.dumps({"nonce": "other", "present": False}) + "\n",
        lambda nonce: json.dumps({"present": False}) + "\n",
        lambda nonce: json.dumps({"nonce": nonce, "present": "no"}) + "\n",
        lambda nonce: json.dumps({"nonce": nonce, "present": False, "extra": 1}) + "\n",
    ],
)
def test_malformed_response_is_an_error_not_missing(make_stdout):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, make_stdout(nonce_of(command)), "")

    vault = make_vault(run)

    with pytest.raises(VaultResponseError):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")


def test_read_without_value_for_present_secret_is_an_error():
    def run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"nonce": nonce_of(command), "present": True}) + "\n", ""
        )

    vault = make_vault(run)

    with pytest.raises(VaultResponseError):
        vault.read_secret_value(DUMMY_SECRET_NAME, "dev")


def test_exists_response_must_not_carry_a_value():
    def run(command, **kwargs):
        response = {"nonce": nonce_of(command), "present": True, "value": DUMMY_SECRET_VALUE}
        return subprocess.CompletedProcess(command, 0, json.dumps(response) + "\n", "")

    vault = make_vault(run)

    with pytest.raises(VaultResponseError):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")


def test_error_messages_do_not_contain_values_or_provider_output():
    vault = make_vault(canned(stdout=DUMMY_SECRET_VALUE, returncode=1))

    with pytest.raises(VaultProviderError) as error:
        vault.read_secret_value(DUMMY_SECRET_NAME, "dev")

    assert DUMMY_SECRET_VALUE not in str(error.value)
    assert "provider error" not in str(error.value)


def test_timeout_is_an_error():
    def run(command, **kwargs):
        assert kwargs["timeout"] == 7
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    vault = make_vault(run, SECRETOPS_PROVIDER_TIMEOUT_SECONDS="7")

    with pytest.raises(VaultTimeoutError):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")


def test_default_timeout_is_applied():
    calls = []
    vault = make_vault(fake_cli({}, calls))
    vault.secret_exists(DUMMY_SECRET_NAME, "dev")

    assert calls[0][1]["timeout"] == 60


@pytest.mark.parametrize("raw", ["0", "-5", "soon"])
def test_invalid_timeout_is_a_configuration_error(raw):
    vault = make_vault(fake_cli({}), SECRETOPS_PROVIDER_TIMEOUT_SECONDS=raw)

    with pytest.raises(VaultConfigurationError):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")


def test_python_variables_in_vault_do_not_change_helper(tmp_path):
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    (module_dir / "json.py").write_text("raise SystemExit(42)\n", encoding="utf-8")
    vault_secrets = {
        DUMMY_SECRET_NAME: DUMMY_SECRET_VALUE,
        "PYTHONPATH": str(module_dir),
        "PYTHONHOME": str(tmp_path / "missing-home"),
    }
    vault = make_vault(fake_cli(vault_secrets))

    assert vault.secret_exists(DUMMY_SECRET_NAME, "dev") is True
    assert vault.read_secret_value(DUMMY_SECRET_NAME, "dev") == DUMMY_SECRET_VALUE


def test_loader_variable_in_helper_environment_is_detected():
    # Inert variable: tests the detection only, not protection against a
    # loader variable that already took effect.
    vault = make_vault(fake_cli({DUMMY_SECRET_NAME: DUMMY_SECRET_VALUE, "LD_SECRETOPS_TEST": "1"}))

    with pytest.raises(VaultProviderError):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")


def test_invalid_secret_names_are_rejected_before_subprocess():
    calls = []
    vault = make_vault(fake_cli({}, calls))

    invalid_names = [
        "X') or __import__('os').system('echo pwned')#",
        "BAD-NAME",
        "bad_lowercase",
        "BAD NAME",
        "BAD/NAME",
        "BAD=NAME",
        "",
    ]
    for name in invalid_names:
        with pytest.raises(ValueError):
            vault.secret_exists(name, "dev")

    assert calls == []


@pytest.mark.parametrize(
    "name",
    ["PATH", "HOME", "USER", "SSL_CERT_FILE", "XDG_CONFIG_HOME", "LC_ALL", "INFISICAL_TOKEN",
     "SECRETOPS_PLAN_DIR", "PYTHONPATH", "LD_PRELOAD"],
)
def test_reserved_secret_names_are_rejected_before_subprocess(name):
    calls = []
    vault = make_vault(fake_cli({}, calls))

    with pytest.raises(ValueError, match="reserved secret name"):
        vault.secret_exists(name, "dev")
    with pytest.raises(ValueError, match="reserved secret name"):
        vault.write_secret(name, "dev", DUMMY_SECRET_VALUE)
    assert calls == []


def test_write_is_confirmed_by_matching_read_back():
    store = {}
    calls = []
    vault = make_vault(fake_cli(store, calls))

    assert vault.write_secret(DUMMY_SECRET_NAME, "dev", DUMMY_SECRET_VALUE) is WriteOutcome.CONFIRMED
    assert store[DUMMY_SECRET_NAME] == DUMMY_SECRET_VALUE
    assert "set" in calls[0][0]
    assert "run" in calls[1][0]


def unknown_write_runner(*, set_exit=0, stored=None, read_exit=0):
    def run(command, **kwargs):
        if "set" in command:
            return subprocess.CompletedProcess(command, set_exit, "", "provider error")
        if read_exit:
            return subprocess.CompletedProcess(command, read_exit, "", "provider error")
        return fake_cli(stored or {})(command, **kwargs)

    return run


@pytest.mark.parametrize(
    "runner",
    [
        unknown_write_runner(set_exit=1),
        unknown_write_runner(set_exit=1, stored={DUMMY_SECRET_NAME: DUMMY_SECRET_VALUE}),
        unknown_write_runner(stored={}),
        unknown_write_runner(stored={DUMMY_SECRET_NAME: "other-value"}),
        unknown_write_runner(read_exit=1),
    ],
    ids=[
        "nonzero-exit",
        "nonzero-exit-even-if-read-back-matches",
        "read-back-missing",
        "read-back-mismatch",
        "read-back-error",
    ],
)
def test_unconfirmed_write_after_cli_start_is_unknown(runner):
    vault = make_vault(runner)

    assert vault.write_secret(DUMMY_SECRET_NAME, "dev", DUMMY_SECRET_VALUE) is WriteOutcome.UNKNOWN


def test_write_timeout_is_unknown_and_removes_temp_file():
    seen = {}

    def run(command, **kwargs):
        seen["temp_path"] = file_arg_path(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    vault = make_vault(run)

    assert vault.write_secret(DUMMY_SECRET_NAME, "dev", DUMMY_SECRET_VALUE) is WriteOutcome.UNKNOWN
    assert not os.path.exists(seen["temp_path"])


def test_write_launch_failure_raises_because_nothing_was_sent():
    def run(command, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", command[0])

    vault = make_vault(run)

    with pytest.raises(VaultProviderError, match="could not be started"):
        vault.write_secret(DUMMY_SECRET_NAME, "dev", DUMMY_SECRET_VALUE)


def write_executable(path):
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_configured_cli_path_is_used(tmp_path):
    binary = write_executable(tmp_path / "infisical")
    calls = []
    vault = InfisicalCliVault(
        runner=fake_cli({}, calls),
        environ={**TEST_ENVIRON, "SECRETOPS_INFISICAL_BIN": str(binary)},
    )

    vault.secret_exists(DUMMY_SECRET_NAME, "dev")

    assert calls[0][0][0] == str(binary)


@pytest.mark.parametrize("kind", ["relative", "missing", "directory", "not_executable"])
def test_invalid_configured_cli_path_stops_before_cli(tmp_path, kind):
    if kind == "relative":
        configured = "bin/infisical"
    elif kind == "missing":
        configured = str(tmp_path / "missing")
    elif kind == "directory":
        configured = str(tmp_path)
    else:
        target = tmp_path / "infisical"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o600)
        configured = str(target)
    calls = []
    vault = InfisicalCliVault(
        runner=fake_cli({}, calls),
        environ={**TEST_ENVIRON, "SECRETOPS_INFISICAL_BIN": configured},
    )

    with pytest.raises(VaultConfigurationError):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")
    assert calls == []


def test_cli_is_never_looked_up_in_caller_path(tmp_path):
    planted_dir = tmp_path / "planted"
    planted_dir.mkdir()
    write_executable(planted_dir / "infisical")
    calls = []
    vault = InfisicalCliVault(
        runner=fake_cli({}, calls),
        environ={**TEST_ENVIRON, "PATH": str(planted_dir)},
    )

    try:
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")
    except VaultConfigurationError:
        assert calls == []
    else:
        assert not calls[0][0][0].startswith(str(planted_dir))


def test_cli_launch_failure_is_a_safe_error():
    def run(command, **kwargs):
        raise PermissionError(13, "Permission denied", command[0])

    vault = make_vault(run)

    with pytest.raises(VaultProviderError, match="could not be started"):
        vault.secret_exists(DUMMY_SECRET_NAME, "dev")


def test_get_vault_returns_infisical_cli_vault():
    vault = get_vault()

    assert isinstance(vault, Vault)
    assert isinstance(vault, InfisicalCliVault)
