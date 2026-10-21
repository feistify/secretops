"""`secretops add`: the human client, against the real broker endpoints.

The broker runs as a local command with fake providers; the terminal is a
scripted stand-in (the real terminal code is covered by the PTY tests).
"""

import json
import os
import subprocess
import sys

import pytest
from test_plans import read_plan, write_plan
from test_secret_add import ADD_NAME, ADD_VALUE, Broker, add_plan, read_record

from secretops import add_client
from secretops.add_client import (
    EXIT_CANCELLED,
    EXIT_CREATED,
    EXIT_NOT_WRITTEN,
    EXIT_UNKNOWN,
    BrokerAccess,
    ClientError,
    load_broker_access,
    run_add,
)
from secretops.terminal_input import InputCancelled, InputRejected, TerminalUnavailable


class ScriptedTerminal:
    def __init__(self, value=ADD_VALUE, confirm=True, value_error=None):
        self.value = value
        self.answer = confirm
        self.value_error = value_error
        self.printed = []
        self.opened = False

    def __call__(self):
        self.opened = True
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read_hidden(self, prompt):
        if self.value_error is not None:
            raise self.value_error
        return self.value

    def confirm(self, prompt):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def print(self, text=""):
        self.printed.append(text)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    started = Broker(tmp_path)
    started.fake_vault()
    for key, value in started.env().items():
        monkeypatch.setenv(key, value)
    return started


def local_access(*wrapper):
    return BrokerAccess(name="local", argv_prefix=(*wrapper, sys.executable, "-m", "secretops.cli"))


def run(terminal, review_args, access=None):
    lines = []
    code = run_add(
        lines.append,
        access=access or local_access(),
        review_args=review_args,
        terminal_factory=terminal,
    )
    return code, lines + terminal.printed


def direct_args(name=ADD_NAME, env="staging"):
    return ["prepare-add", "--name", name, "--env", env]


def only_plan(broker):
    [path] = broker.plan_dir.glob("*.json")
    return read_plan(path)


def test_direct_entry_saves_and_verifies(broker):
    terminal = ScriptedTerminal()

    code, lines = run(terminal, direct_args())

    assert code == EXIT_CREATED
    assert "Saved and verified." in lines
    assert "Checks passed. The secret is currently absent." in lines
    assert any(line.startswith("Vault:       infisical / dummy-project / staging / /") for line in lines)
    assert any(line.startswith("Server:      https://eu.infisical.com") for line in lines)
    assert f"Secret:      {ADD_NAME}" in lines
    assert "Operation:   create only; refused if present at the final check" in lines
    assert read_record(broker.record_path)["stored"][ADD_NAME] == ADD_VALUE
    assert only_plan(broker)["status"] == "executed"
    assert all(ADD_VALUE not in line for line in lines)


def test_agent_prepared_plan_is_completed(broker):
    write_plan(broker.plan_dir, add_plan())

    code, lines = run(ScriptedTerminal(), ["review-add", "--id", "add-plan"])

    assert code == EXIT_CREATED
    assert "Plan:        add-plan" in lines
    assert read_plan(broker.plan_path("add-plan"))["status"] == "executed"


def test_existing_secret_stops_before_the_value_prompt(broker, tmp_path):
    exists = Broker(tmp_path)
    exists.fake_vault(existing=[ADD_NAME])
    terminal = ScriptedTerminal()

    code, lines = run(terminal, direct_args())

    assert code == EXIT_NOT_WRITTEN
    assert "Secret already exists. Nothing was written." in lines
    assert terminal.opened is False


def test_declined_confirmation_sends_nothing(broker):
    code, lines = run(ScriptedTerminal(confirm=False), direct_args())

    assert code == EXIT_CANCELLED
    assert "Cancelled. Nothing was sent." in lines
    assert "set" not in read_record(broker.record_path)["calls"]
    assert only_plan(broker)["status"] == "planned"


@pytest.mark.parametrize(
    "terminal",
    [
        ScriptedTerminal(value_error=InputCancelled("cancelled")),
        ScriptedTerminal(confirm=KeyboardInterrupt()),
    ],
    ids=["cancel-at-value", "interrupt-at-confirmation"],
)
def test_cancellation_before_submission_sends_nothing(broker, terminal):
    code, lines = run(terminal, direct_args())

    assert code == EXIT_CANCELLED
    assert "Cancelled. Nothing was sent." in lines
    assert "set" not in read_record(broker.record_path)["calls"]
    assert only_plan(broker)["status"] == "planned"


@pytest.mark.parametrize(
    "terminal",
    [
        ScriptedTerminal(value=""),
        ScriptedTerminal(value="   "),
        ScriptedTerminal(value="a\tb"),
        ScriptedTerminal(value_error=InputRejected("multiline values are not supported; nothing was saved")),
    ],
    ids=["empty", "whitespace", "control", "multiline"],
)
def test_invalid_input_is_refused_locally_and_plan_is_reusable(broker, terminal):
    code, lines = run(terminal, direct_args())

    assert code == EXIT_NOT_WRITTEN
    assert any(line.startswith("Not saved:") for line in lines)
    assert any("can be used again" in line for line in lines)
    assert "set" not in read_record(broker.record_path)["calls"]
    assert only_plan(broker)["status"] == "planned"


def test_missing_terminal_sends_nothing(broker):
    code, _ = run(ScriptedTerminal(value_error=TerminalUnavailable("no terminal")), direct_args())

    assert code == EXIT_NOT_WRITTEN
    assert "set" not in read_record(broker.record_path)["calls"]


def test_lost_submission_response_is_unknown(broker, tmp_path):
    # The broker is reached for review, but the submission dies without a
    # response after the value was sent.
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import os, sys\n"
        "if 'submit-add' in sys.argv:\n"
        "    sys.stdin.buffer.read()\n"
        "    sys.exit(255)\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    access = BrokerAccess(
        name="local", argv_prefix=(sys.executable, str(wrapper), "-m", "secretops.cli")
    )

    code, lines = run(ScriptedTerminal(), direct_args(), access=access)

    assert code == EXIT_UNKNOWN
    assert "Write outcome is unknown. Do not submit again yet." in lines
    assert any("Check broker status for plan" in line for line in lines)


def test_unknown_broker_outcome_is_reported(broker, tmp_path):
    unknown = Broker(tmp_path)
    unknown.fake_vault(store_other=True)

    code, lines = run(ScriptedTerminal(), direct_args())

    assert code == EXIT_UNKNOWN
    assert "Write outcome is unknown. Do not submit again yet." in lines


def test_value_travels_only_in_the_submission_body(broker, tmp_path):
    log = tmp_path / "broker-calls.jsonl"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import json, os, sys\n"
        f"log = open({str(log)!r}, 'a', encoding='utf-8')\n"
        "log.write(json.dumps({'argv': sys.argv, 'env': sorted(os.environ.values())}) + '\\n')\n"
        "log.close()\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    access = BrokerAccess(
        name="local", argv_prefix=(sys.executable, str(wrapper), "-m", "secretops.cli")
    )

    code, _ = run(ScriptedTerminal(), direct_args(), access=access)

    assert code == EXIT_CREATED
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [call["argv"][3] for call in calls] == ["prepare-add", "submit-add"]
    for call in calls:
        assert all(ADD_VALUE not in part for part in call["argv"])
        assert all(ADD_VALUE not in value for value in call["env"])


def test_broker_text_is_rendered_without_control_characters():
    assert add_client.safe_text("ok\x1b[2Jname\x07") == "ok?[2Jname?"


def test_review_with_unexpected_fields_is_not_trusted():
    raw = json.dumps({
        "protocol": 1, "outcome": "ready", "message": "", "plan": None,
        "digest": "sha256-v1:x", "atomic_create": False, "extra": 1,
    }).encode() + b"\n"

    with pytest.raises(ValueError):
        add_client.parse_review_response(raw)


# Broker access configuration


def write_config(tmp_path, monkeypatch, text):
    path = tmp_path / "client.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("SECRETOPS_CLIENT_CONFIG", str(path))


def test_ssh_access_uses_no_pty_and_no_forwarding(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, "brokers:\n  nas:\n    ssh: secretops-broker@nas\n")

    access = load_broker_access(None)

    argv = access.argv("review-add", "--id", "abc")
    assert argv[:2] == ["ssh", "-T"]
    assert "RequestTTY=no" in argv and "ForwardAgent=no" in argv
    assert "StrictHostKeyChecking=no" not in " ".join(argv)
    assert argv[-5:] == ["secretops-broker@nas", "/usr/local/bin/secretops-broker", "review-add", "--id", "abc"]


def test_ssh_to_a_personal_account_then_sudo(tmp_path, monkeypatch):
    write_config(
        tmp_path, monkeypatch,
        "brokers:\n  nas:\n    ssh: alice@nas\n    sudo_user: secretops-broker\n",
    )

    argv = load_broker_access(None).argv("submit-add")

    assert argv[:2] == ["ssh", "-T"] and "RequestTTY=no" in argv
    assert argv[-8:] == [
        "alice@nas", "sudo", "-n", "-u", "secretops-broker", "--",
        "/usr/local/bin/secretops-broker", "submit-add",
    ]


def test_sudo_access_runs_the_installed_broker_command(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, "brokers:\n  laptop:\n    sudo_user: secretops-broker\n")

    access = load_broker_access("laptop")

    assert access.argv("submit-add") == [
        "sudo", "-u", "secretops-broker", "--", "/usr/local/bin/secretops-broker", "submit-add",
    ]


def test_several_brokers_need_an_explicit_choice(tmp_path, monkeypatch):
    write_config(
        tmp_path, monkeypatch,
        "brokers:\n  a:\n    ssh: host-a\n  b:\n    ssh: host-b\n",
    )

    with pytest.raises(ClientError, match="choose one with --broker: a, b"):
        load_broker_access(None)
    assert load_broker_access("b").name == "b"


def test_default_broker_is_used(tmp_path, monkeypatch):
    write_config(
        tmp_path, monkeypatch,
        "default_broker: b\nbrokers:\n  a:\n    ssh: host-a\n  b:\n    ssh: host-b\n",
    )

    assert load_broker_access(None).name == "b"


def test_missing_configuration_explains_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETOPS_CLIENT_CONFIG", str(tmp_path / "missing.yaml"))

    with pytest.raises(ClientError, match="no broker is configured"):
        load_broker_access(None)


@pytest.mark.parametrize(
    "entry",
    [
        "ssh: '-oProxyCommand=evil'",
        "ssh: 'host; rm'",
        "ssh: host\n    command: [a]",
        "ssh: host\n    sudo_user: '-u'",
        "ssh: host\n    broker_command: 'secretops-broker'",
        "ssh: host\n    broker_command: '/usr/bin/x; id'",
        "sudo_user: '-s'",
        "command: 'not a list'",
    ],
)
def test_unsafe_access_configuration_is_refused(tmp_path, monkeypatch, entry):
    write_config(tmp_path, monkeypatch, f"brokers:\n  x:\n    {entry}\n")

    with pytest.raises(ClientError):
        load_broker_access(None)


# Command line


def run_cli_add(broker, tmp_path, *args):
    config = tmp_path / "client.yaml"
    config.write_text(
        "brokers:\n  local:\n    command: [" + json.dumps(sys.executable) + ", -m, secretops.cli]\n",
        encoding="utf-8",
    )
    env = broker.env()
    env["SECRETOPS_CLIENT_CONFIG"] = str(config)
    # A new session has no controlling terminal: hidden input is unavailable.
    return subprocess.run(
        [sys.executable, "-m", "secretops.cli", "add", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        start_new_session=True,
        check=False,
    )


def test_cli_refuses_plan_with_name_or_env(broker, tmp_path):
    result = run_cli_add(broker, tmp_path, ADD_NAME, "--plan", "add-plan")

    assert result.returncode == EXIT_NOT_WRITTEN
    assert "--plan cannot be combined" in result.stdout


def test_cli_without_terminal_refuses_before_sending(broker, tmp_path):
    result = run_cli_add(broker, tmp_path, ADD_NAME, "--env", "staging")

    assert result.returncode == EXIT_NOT_WRITTEN
    assert "Checks passed" in result.stdout
    assert "no terminal is available for hidden input" in result.stdout
    assert "Nothing was sent." in result.stdout
    assert "set" not in read_record(broker.record_path)["calls"]
    assert only_plan(broker)["status"] == "planned"


def test_cli_rejects_invalid_names_before_contacting_the_broker(broker, tmp_path):
    result = run_cli_add(broker, tmp_path, "bad-name", "--env", "staging")

    assert result.returncode == EXIT_NOT_WRITTEN
    assert not list(broker.plan_dir.glob("*.json"))
    assert os.path.exists(broker.record_path) is False or read_record(broker.record_path)["calls"] == []
