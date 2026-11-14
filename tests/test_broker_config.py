import os
import shlex
import subprocess

import pytest

from secretops import broker_config
from secretops.broker_config import (
    TOKEN_KEY,
    ConfigError,
    configure,
    parse,
    read_env,
    serialize,
    write_env,
)

TOKEN = "synthetic-token-value"


def env_text(**values):
    return serialize(values)


def answers(*replies):
    queue = list(replies)
    return lambda prompt: queue.pop(0)


def test_serialize_and_parse_round_trip_hostile_values():
    values = {
        "INFISICAL_DOMAIN": "https://vault.example.com",
        "INFISICAL_TOKEN": "a'b\"c$(touch pwned)`x`;rm -rf /",
        "SECRETOPS_INFISICAL_PATH": "/",
    }
    assert parse(serialize(values)) == values


def test_written_file_means_the_same_to_the_shell(tmp_path):
    marker = tmp_path / "pwned"
    hostile = f"$(touch {marker})`touch {marker}`'\";"
    env = tmp_path / "broker.env"
    env.write_text(serialize({"INFISICAL_TOKEN": hostile}))
    result = subprocess.run(
        ["sh", "-c", 'set -a; . "$1"; printf %s "$INFISICAL_TOKEN"', "sh", str(env)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == hostile
    assert not marker.exists()


@pytest.mark.parametrize(
    "line",
    [
        'INFISICAL_TOKEN="$(id)"',
        "INFISICAL_TOKEN=$(id)",
        "INFISICAL_TOKEN=a b",
        "INFISICAL_TOKEN='unterminated",
        "INFISICAL_TOKEN=",
        "export INFISICAL_TOKEN=x",
        "GH_TOKEN=x",
        "INFISICAL_TOKEN=x # comment",
    ],
)
def test_parse_refuses_non_canonical_lines_without_echoing_them(line):
    with pytest.raises(ConfigError) as error:
        parse(f"SECRETOPS_INFISICAL_PATH=/\n{line}\n")
    assert "line 2" in str(error.value)
    value = line.split("=", 1)[1]
    assert not value or value not in str(error.value)


def test_parse_refuses_duplicates_and_allows_comments():
    assert parse("# managed\n\nSECRETOPS_INFISICAL_PATH=/\n") == {"SECRETOPS_INFISICAL_PATH": "/"}
    with pytest.raises(ConfigError, match="duplicate"):
        parse("SECRETOPS_INFISICAL_PATH=/\nSECRETOPS_INFISICAL_PATH=/a\n")


def test_serialize_refuses_multiline_values():
    with pytest.raises(ConfigError):
        serialize({"INFISICAL_TOKEN": "a\nSECRETOPS_BIN=/tmp/evil"})


def test_configure_full_keeps_layout_settings():
    current = {**broker_config.LAYOUT_DEFAULTS, "INFISICAL_DOMAIN": "https://old.example.com"}
    updated = configure(
        current,
        token_only=False,
        read=answers("https://vault.example.com/", "project-1", "/app"),
        read_hidden=answers(TOKEN),
    )
    assert updated["INFISICAL_DOMAIN"] == "https://vault.example.com"
    assert updated["SECRETOPS_INFISICAL_PROJECT_ID"] == "project-1"
    assert updated["SECRETOPS_INFISICAL_PATH"] == "/app"
    assert updated["INFISICAL_TOKEN"] == TOKEN
    assert updated["SECRETOPS_PLAN_DIR"] == "/opt/secretops/plans"


def test_configure_enter_keeps_current_values_and_token():
    current = {
        "INFISICAL_DOMAIN": "https://vault.example.com",
        "SECRETOPS_INFISICAL_PROJECT_ID": "project-1",
        "SECRETOPS_INFISICAL_PATH": "/",
        "INFISICAL_TOKEN": TOKEN,
    }
    assert configure(current, token_only=False, read=answers("", "", ""), read_hidden=answers("")) == current


def test_token_only_changes_only_the_token():
    current = {"INFISICAL_DOMAIN": "https://vault.example.com", "INFISICAL_TOKEN": "old"}
    updated = configure(current, token_only=True, read=answers(), read_hidden=answers(TOKEN))
    assert updated == {**current, "INFISICAL_TOKEN": TOKEN}


@pytest.mark.parametrize("token", ["", "two words", "line\nbreak", "nul\0byte"])
def test_token_only_refuses_invalid_tokens(token):
    with pytest.raises(ConfigError) as error:
        configure({"INFISICAL_TOKEN": "old"}, token_only=True, read=answers(), read_hidden=answers(token))
    assert token.strip() == "" or token not in str(error.value)


def test_first_configuration_requires_a_token():
    with pytest.raises(ConfigError):
        configure({}, token_only=False, read=answers("https://v.example.com", "p", "/"), read_hidden=answers(""))


@pytest.mark.parametrize(
    "domain",
    ["ftp://vault", "https://user:pass@vault.example.com", "vault.example.com", "https://v.example.com/?a=1"],
)
def test_domain_validation(domain):
    with pytest.raises(ConfigError) as error:
        broker_config.validate_domain(domain)
    assert "pass" not in str(error.value)


@pytest.mark.parametrize("path", ["app", "/a b", "/a/../b", "//a"])
def test_vault_path_validation(path):
    with pytest.raises(ConfigError):
        broker_config.validate_vault_path(path)


@pytest.mark.parametrize(
    ("domain", "warn"),
    [
        ("http://127.0.0.1:8086", False),
        ("http://localhost:8086", False),
        ("https://vault.example.com", False),
        ("http://192.0.2.10:8086", True),
        ("http://vault.lan", True),
    ],
)
def test_unencrypted_remote_warning(domain, warn):
    assert broker_config.is_unencrypted_remote(domain) is warn


def test_write_env_is_private_and_replaces_atomically(tmp_path):
    env = tmp_path / "broker.env"
    env.write_text("old\n")
    write_env(env, {"INFISICAL_TOKEN": TOKEN}, os.getuid(), os.getgid())
    assert env.stat().st_mode & 0o777 == 0o600
    assert read_env(env, os.getuid()) == {"INFISICAL_TOKEN": TOKEN}
    assert [path.name for path in tmp_path.iterdir()] == ["broker.env"]


def test_read_env_refuses_symlinks_broad_modes_and_other_owners(tmp_path):
    env = tmp_path / "broker.env"
    env.write_text(env_text(INFISICAL_TOKEN=TOKEN))
    env.chmod(0o644)
    with pytest.raises(ConfigError, match="0600"):
        read_env(env, os.getuid())
    env.chmod(0o600)
    with pytest.raises(ConfigError, match="owned"):
        read_env(env, os.getuid() + 1)
    link = tmp_path / "link.env"
    link.symlink_to(env)
    with pytest.raises(ConfigError, match="symlink"):
        read_env(link, os.getuid())


def test_main_requires_root_and_prints_no_values(monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert broker_config.main([]) == 1
    captured = capsys.readouterr()
    assert "sudo" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_main_refuses_non_interactive_input(monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(broker_config.pwd, "getpwnam", lambda name: _account())
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert broker_config.main(["--token-only"]) == 1
    assert "interactive terminal" in capsys.readouterr().err


def _account():
    import pwd

    return pwd.getpwuid(os.getuid())


def test_installer_init_values_need_trusted_paths(tmp_path):
    with pytest.raises(ConfigError):
        broker_config.init_values("https://v.example.com", str(tmp_path / "missing"), None)
    values = broker_config.init_values("https://v.example.com/", "/bin/sh", None)
    assert values["INFISICAL_DOMAIN"] == "https://v.example.com"
    assert "INFISICAL_TOKEN" not in values
    assert "SECRETOPS_GH_BIN" not in values
    assert shlex.quote(values["SECRETOPS_POLICY_PATH"]) == "/opt/secretops/policy.yaml"


def test_hidden_input_refuses_visible_fallback(monkeypatch):
    import getpass
    import warnings

    typed = []

    def fallback(prompt):
        # What getpass does when it cannot turn echo off: warn, then read
        # visibly. The warning must stop it before anything is read.
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
        typed.append("read")
        return TOKEN

    monkeypatch.setattr(getpass, "getpass", fallback)
    with pytest.raises(ConfigError, match="hidden input"):
        broker_config.hidden_input("Token: ")
    assert typed == []


class _Recorder:
    def __init__(self, returncode=0, error=None, output=b""):
        self.returncode = returncode
        self.error = error
        self.output = output
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.returncode, self.output)


def test_check_runs_as_broker_user_without_credentials(capsys):
    account = _account()
    runner = _Recorder(returncode=1, output=b"FAIL vault not reachable \xff\n")
    assert broker_config.run_check(account, "staging", 95, runner=runner) == 1
    (command, kwargs), = runner.calls
    assert command == ["/usr/local/bin/secretops-broker", "check-vault-connection", "--env", "staging"]
    assert kwargs["user"] == account.pw_uid and kwargs["group"] == account.pw_gid
    assert kwargs["extra_groups"] == []
    assert set(kwargs["env"]) == {"PATH", "HOME", "LANG"}
    # Isolated from root's terminal: new session, no stdin, output piped.
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE and kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["cwd"] == "/"
    assert kwargs.get("shell", False) is False
    assert kwargs["timeout"] == 95
    out = capsys.readouterr().out
    assert "staging" in out
    assert "FAIL vault not reachable \ufffd" in out


def test_check_timeout_follows_the_provider_timeout():
    assert broker_config.check_timeout({}) == 90
    assert broker_config.check_timeout({"SECRETOPS_PROVIDER_TIMEOUT_SECONDS": "120"}) == 150
    assert broker_config.check_timeout({"SECRETOPS_PROVIDER_TIMEOUT_SECONDS": "x"}) == 90


def test_check_timeout_is_reported(capsys):
    runner = _Recorder(error=subprocess.TimeoutExpired("secretops-broker", 90))
    assert broker_config.run_check(_account(), "dev", 90, runner=runner) == 1
    assert "did not finish within 90 seconds" in capsys.readouterr().err


def test_interrupted_check_is_reported_without_traceback(capsys):
    runner = _Recorder(error=KeyboardInterrupt())
    assert broker_config.run_check(_account(), "dev", runner=runner) == broker_config.CHECK_INTERRUPTED
    assert "interrupted" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["dev", "staging", "prod-eu", "e2e_1.x"])
def test_env_names_accepted(name):
    assert broker_config.validate_env_name(name) == name


@pytest.mark.parametrize("name", ["", "-dev", ".dev", "_dev", "dev;id", "a" * 65])
def test_env_names_refused(name):
    with pytest.raises(ConfigError):
        broker_config.validate_env_name(name)


def test_check_that_cannot_start_fails_safely(capsys):
    runner = _Recorder(error=FileNotFoundError("/usr/local/bin/secretops-broker"))
    assert broker_config.run_check(_account(), "dev", runner=runner) == 1
    assert "could not start" in capsys.readouterr().err


@pytest.fixture
def interactive_root(monkeypatch, tmp_path):
    env_file = tmp_path / "broker.env"
    write_env(env_file, {"INFISICAL_TOKEN": "old"}, os.getuid(), os.getgid())
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(broker_config.pwd, "getpwnam", lambda name: _account())
    monkeypatch.setattr(broker_config, "BROKER_ENV", env_file)
    checks = []

    def fake_check(account, env, timeout=None, runner=None):
        checks.append(env)
        return fake_check.returncode

    fake_check.returncode = 0
    monkeypatch.setattr(broker_config, "run_check", fake_check)
    return env_file, checks, fake_check


@pytest.mark.parametrize("check_code", [0, 1, broker_config.CHECK_INTERRUPTED])
def test_check_runs_after_save_and_failure_keeps_the_save(interactive_root, monkeypatch, capsys, check_code):
    env_file, checks, fake_check = interactive_root
    fake_check.returncode = check_code
    # Set here, in the test call: pytest swaps the captured streams per phase.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        broker_config, "configure",
        lambda values, token_only: {**values, "INFISICAL_DOMAIN": "https://v.example.com", TOKEN_KEY: TOKEN},
    )
    assert broker_config.main(["--env", "staging"]) == 0
    assert checks == ["staging"]
    assert read_env(env_file, os.getuid())[TOKEN_KEY] == TOKEN
    out = capsys.readouterr().out
    assert "Broker configuration saved" in out
    assert ("vault check failed" in out) is (check_code == 1)
    assert ("vault check was interrupted" in out) is (check_code == broker_config.CHECK_INTERRUPTED)
    if check_code:
        assert "sudo secretops-configure --check --env staging" in out
    assert TOKEN not in out


def test_check_option_only_checks(interactive_root, monkeypatch):
    env_file, checks, fake_check = interactive_root
    fake_check.returncode = 1
    before = env_file.read_text()
    monkeypatch.setattr(broker_config, "configure", lambda *a, **k: pytest.fail("must not configure"))
    assert broker_config.main(["--check"]) == 1
    assert checks == ["dev"]
    assert env_file.read_text() == before


@pytest.mark.parametrize("argv", [["--check", "--token-only"], ["--check", "--env", "dev;id"]])
def test_check_option_refuses_bad_combinations(interactive_root, argv, capsys):
    _, checks, _ = interactive_root
    assert broker_config.main(argv) == 1
    assert checks == []
    assert "secretops-configure:" in capsys.readouterr().err


@pytest.mark.parametrize(("typed", "told"), [(TOKEN, [f"Input received: {len(TOKEN)} characters."]), ("", [])])
def test_hidden_input_reports_only_the_count(monkeypatch, capsys, typed, told):
    import getpass

    lines = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt: typed)
    monkeypatch.setattr(broker_config, "tell_terminal", lines.append)
    assert broker_config.hidden_input("Token: ") == typed
    assert lines == told
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err + "".join(lines)


def test_refused_hidden_input_reports_nothing(monkeypatch):
    import getpass
    import warnings

    lines = []

    def fallback(prompt):
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
        return TOKEN

    monkeypatch.setattr(getpass, "getpass", fallback)
    monkeypatch.setattr(broker_config, "tell_terminal", lines.append)
    with pytest.raises(ConfigError):
        broker_config.hidden_input("Token: ")
    assert lines == []


def test_tell_terminal_falls_back_to_stderr(monkeypatch, capsys):
    def no_tty(*args, **kwargs):
        raise OSError("no terminal")

    monkeypatch.setattr(broker_config.os, "open", no_tty)
    broker_config.tell_terminal("Input received: 3 characters.")
    assert capsys.readouterr().err == "Input received: 3 characters.\n"
