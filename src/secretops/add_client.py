"""`secretops add`: the human client for interactive secret entry.

The client holds no vault credentials. It calls the broker's human endpoints
through the human's own broker access (SSH or sudo), shows the broker's
validated plan, reads the value from the terminal, and sends it once, in
the body of the submission request on the endpoint's stdin.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from secretops.human_entry import (
    PROTOCOL_VERSION,
    REVIEW_OUTCOMES,
    SUBMIT_OUTCOMES,
    InvalidValueError,
    validate_entered_value,
)
from secretops.terminal_input import (
    HiddenTerminal,
    InputCancelled,
    InputRejected,
    TerminalUnavailable,
)

CLIENT_CONFIG_ENV = "SECRETOPS_CLIENT_CONFIG"
DEFAULT_BROKER_COMMAND = "/usr/local/bin/secretops-broker"
ENDPOINT_TIMEOUT_SECONDS = 300

EXIT_CREATED = 0
EXIT_NOT_WRITTEN = 1
EXIT_CANCELLED = 2
EXIT_UNKNOWN = 3

SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.@:+-]+")
SAFE_PATH = re.compile(r"/[A-Za-z0-9_./-]*")
PLAN_FIELDS = (
    "plan_id",
    "secret_name",
    "env",
    "create_mode",
    "value_source",
    "vault_backend",
    "vault_domain",
    "vault_project_id",
    "vault_path",
)
SETUP_HINT = (
    "configure human broker access in ~/.config/secretops/client.yaml "
    "(see docs/BROKER_HOST.md, 'Human secret entry')"
)


class ClientError(Exception):
    """A problem before anything was sent. Safe to print."""


@dataclass(frozen=True)
class BrokerAccess:
    name: str
    argv_prefix: tuple[str, ...]

    def argv(self, *args: str) -> list[str]:
        return [*self.argv_prefix, *args]


def client_config_path() -> Path:
    configured = os.environ.get(CLIENT_CONFIG_ENV)
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "secretops" / "client.yaml"


def _broker_command(entry: dict) -> str:
    command = entry.get("broker_command", DEFAULT_BROKER_COMMAND)
    if not isinstance(command, str) or not SAFE_PATH.fullmatch(command):
        raise ClientError("broker_command must be an absolute path without spaces")
    return command


def _access_from_entry(name: str, entry: Any) -> BrokerAccess:
    if not isinstance(entry, dict):
        raise ClientError(f"broker {name} is not configured correctly")
    if "command" in entry and ("ssh" in entry or "sudo_user" in entry):
        raise ClientError(f"broker {name}: command cannot be combined with ssh or sudo_user")
    if not any(kind in entry for kind in ("ssh", "sudo_user", "command")):
        raise ClientError(f"broker {name} needs ssh, sudo_user, or command")
    if "sudo_user" in entry:
        user = entry["sudo_user"]
        if not isinstance(user, str) or not SAFE_TOKEN.fullmatch(user) or user.startswith("-"):
            raise ClientError(f"broker {name}: sudo_user is not valid")
    if "ssh" in entry:
        destination = entry["ssh"]
        if not isinstance(destination, str) or not SAFE_TOKEN.fullmatch(destination) or destination.startswith("-"):
            raise ClientError(f"broker {name}: ssh must be a host or user@host")
        remote = (_broker_command(entry),)
        if "sudo_user" in entry:
            # Personal account on the broker host, then sudo to the broker
            # user. Without a remote PTY sudo cannot ask for a password, so
            # -n fails instead of prompting; a narrow sudoers rule is needed.
            remote = ("sudo", "-n", "-u", entry["sudo_user"], "--", *remote)
        # No remote PTY and no forwarding; host-key checking stays as configured.
        prefix = (
            "ssh", "-T",
            "-o", "RequestTTY=no",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "ClearAllForwardings=yes",
            destination,
            *remote,
        )
    elif "sudo_user" in entry:
        prefix = ("sudo", "-u", entry["sudo_user"], "--", _broker_command(entry))
    else:
        command = entry["command"]
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise ClientError(f"broker {name}: command must be a list of arguments")
        prefix = tuple(command)
    return BrokerAccess(name=name, argv_prefix=prefix)


def load_broker_access(selected: str | None) -> BrokerAccess:
    path = client_config_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as error:
        raise ClientError(f"no broker is configured; {SETUP_HINT}") from error
    except (OSError, yaml.YAMLError) as error:
        raise ClientError(f"cannot read {path}") from error
    brokers = data.get("brokers") if isinstance(data, dict) else None
    if not isinstance(brokers, dict) or not brokers:
        raise ClientError(f"no broker is configured; {SETUP_HINT}")
    name = selected or data.get("default_broker")
    if name is None:
        if len(brokers) != 1:
            raise ClientError(
                "several brokers are configured; choose one with --broker: "
                + ", ".join(sorted(str(key) for key in brokers))
            )
        name = next(iter(brokers))
    if name not in brokers:
        raise ClientError(f"broker {name} is not configured")
    return _access_from_entry(str(name), brokers[name])


def safe_text(value: Any) -> str:
    """Render broker text without control characters that could alter the terminal."""
    return "".join(
        character if character.isprintable() else "?" for character in str(value)
    )


def _parse_response(stdout: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8")
    if not text.endswith("\n") or "\n" in text[:-1]:
        raise ValueError("response is not one line")
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("unsupported response")
    if not isinstance(data.get("outcome"), str) or not isinstance(data.get("message"), str):
        raise ValueError("unexpected response")
    return data


def parse_review_response(stdout: bytes) -> dict[str, Any]:
    data = _parse_response(stdout)
    if set(data) != {"protocol", "outcome", "message", "plan", "digest", "atomic_create"}:
        raise ValueError("unexpected review response")
    if data["outcome"] not in REVIEW_OUTCOMES:
        raise ValueError("unexpected review outcome")
    plan = data["plan"]
    if plan is not None and (
        not isinstance(plan, dict)
        or set(plan) != set(PLAN_FIELDS)
        or not all(isinstance(value, str) for value in plan.values())
    ):
        raise ValueError("unexpected plan metadata")
    if data["outcome"] == "ready" and (
        plan is None
        or not isinstance(data["digest"], str)
        or not isinstance(data["atomic_create"], bool)
    ):
        raise ValueError("incomplete review")
    return data


def parse_submit_response(stdout: bytes) -> dict[str, Any]:
    data = _parse_response(stdout)
    if set(data) != {"protocol", "outcome", "message", "plan_id"}:
        raise ValueError("unexpected submit response")
    if data["outcome"] not in SUBMIT_OUTCOMES:
        raise ValueError("unexpected submit outcome")
    return data


def call_review(access: BrokerAccess, args: list[str]) -> dict[str, Any]:
    """Call prepare-add or review-add. Nothing secret is sent."""
    try:
        result = subprocess.run(
            access.argv(*args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            timeout=ENDPOINT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ClientError(f"cannot reach broker {access.name}") from error
    try:
        return parse_review_response(result.stdout)
    except (UnicodeDecodeError, ValueError) as error:
        raise ClientError(
            f"broker {access.name} did not return a valid review "
            f"(exit status {result.returncode}); check the human broker access"
        ) from error


def call_submit(access: BrokerAccess, request: bytes) -> dict[str, Any] | None:
    """Send the submission once. None means the outcome is unknown."""
    try:
        result = subprocess.run(
            access.argv("submit-add"),
            input=request,
            stdout=subprocess.PIPE,
            timeout=ENDPOINT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return parse_submit_response(result.stdout)
    except (UnicodeDecodeError, ValueError):
        return None


def operation_text(atomic_create: bool) -> str:
    if atomic_create:
        return "create only; an existing secret is never replaced"
    return "create only; refused if present at the final check"


def summary_lines(access: BrokerAccess, review: dict[str, Any]) -> list[str]:
    plan = {key: safe_text(value) for key, value in review["plan"].items()}
    return [
        f"Broker:      {safe_text(access.name)}",
        f"Vault:       {plan['vault_backend']} / {plan['vault_project_id']} / {plan['env']} / {plan['vault_path']}",
        f"Server:      {plan['vault_domain']}",
        f"Secret:      {plan['secret_name']}",
        f"Operation:   {operation_text(review['atomic_create'])}",
        f"Plan:        {plan['plan_id']}",
    ]


def unknown_lines(plan_id: str | None) -> list[str]:
    lines = ["Write outcome is unknown. Do not submit again yet."]
    if plan_id:
        lines.append(
            f"Check broker status for plan {safe_text(plan_id)}; "
            "if unresolved, ask the operator to verify it."
        )
    return lines


def run_add(echo, *, access: BrokerAccess, review_args: list[str],
            terminal_factory=HiddenTerminal) -> int:
    """Review, read the value, confirm, submit. Returns the exit code.

    `echo` prints one line of safe text to the user.
    """
    review = call_review(access, review_args)
    outcome = review["outcome"]
    if outcome == "ready":
        for line in summary_lines(access, review):
            echo(line)
    if outcome == "exists":
        echo("Secret already exists. Nothing was written.")
        return EXIT_NOT_WRITTEN
    if outcome != "ready":
        echo(f"Not saved: {safe_text(review['message'])}")
        return EXIT_NOT_WRITTEN
    plan_id = review["plan"]["plan_id"]
    echo("")
    echo("Checks passed. The secret is currently absent.")

    try:
        with terminal_factory() as terminal:
            try:
                value = terminal.read_hidden("Value (hidden): ")
                validate_entered_value(value)
            except (InputRejected, InvalidValueError) as error:
                terminal.print(f"Not saved: {error}")
                terminal.print(f"Nothing was sent. The plan {plan_id} can be used again.")
                return EXIT_NOT_WRITTEN
            for line in summary_lines(access, review)[3:5]:
                terminal.print(line)
            if not terminal.confirm("Save? [y/N]: "):
                terminal.print("Cancelled. Nothing was sent.")
                return EXIT_CANCELLED
    except InputCancelled:
        echo("Cancelled. Nothing was sent.")
        return EXIT_CANCELLED
    except TerminalUnavailable as error:
        echo(f"Not saved: {error}. Nothing was sent.")
        return EXIT_NOT_WRITTEN
    except KeyboardInterrupt:
        echo("Cancelled. Nothing was sent.")
        return EXIT_CANCELLED

    request = (
        json.dumps(
            {
                "protocol": PROTOCOL_VERSION,
                "plan_id": plan_id,
                "digest": review["digest"],
                "value": value,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    del value
    try:
        response = call_submit(access, request)
    except KeyboardInterrupt:
        response = None
    del request
    if response is None:
        for line in unknown_lines(plan_id):
            echo(line)
        return EXIT_UNKNOWN
    result = response["outcome"]
    if result == "created":
        echo("")
        echo("Saved and verified.")
        return EXIT_CREATED
    if result == "exists":
        echo("Secret already exists. Nothing was written by this invocation.")
        return EXIT_NOT_WRITTEN
    if result == "unknown":
        for line in unknown_lines(plan_id):
            echo(line)
        return EXIT_UNKNOWN
    echo(f"Not saved: {safe_text(response['message'])}")
    return EXIT_NOT_WRITTEN
