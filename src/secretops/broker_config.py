"""Human-only helper that writes the broker's broker.env.

Run as root from the human's own terminal (installed as
/usr/local/sbin/secretops-configure). The broker wrapper sources broker.env
as shell, so this module never sources it: it reads only lines in the exact
form it writes itself, KEY=<shlex.quote(value)>, for known keys, and writes
every value with shlex.quote. Errors name a line number, never its content.

    secretops-configure                 # destination settings and token
    secretops-configure --token-only    # replace only the token
    secretops-configure --check         # check the saved configuration

After saving, and with --check, the vault connection is checked as the
broker user through the broker wrapper (`check-vault-connection`), for the
environment given with --env (default dev). That command reads broker.env
itself; this helper passes it no credential. A failed check does not undo
the save. The installer uses --init once to write the initial file without
a token. No credential is printed.
"""

import argparse
import getpass
import ipaddress
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from secretops.terminal_input import input_received_line

BROKER_ENV = Path("/opt/secretops/broker.env")
BROKER_USER = "secretops-broker"
BROKER_COMMAND = "/usr/local/bin/secretops-broker"
CHECK_PATH = "/usr/bin:/bin"
DEFAULT_CHECK_ENV = "dev"
ENV_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# The check makes one provider call plus a short unauthenticated probe.
DEFAULT_PROVIDER_TIMEOUT = 60
PROBE_TIMEOUT = 10
CHECK_MARGIN_SECONDS = 20
CHECK_INTERRUPTED = 130
TOKEN_KEY = "INFISICAL_TOKEN"
# Written in this order; unknown keys are refused.
KNOWN_KEYS = (
    "SECRETOPS_BIN",
    "INFISICAL_DOMAIN",
    TOKEN_KEY,
    "SECRETOPS_INFISICAL_PROJECT_ID",
    "SECRETOPS_INFISICAL_PATH",
    "SECRETOPS_INFISICAL_BIN",
    "SECRETOPS_GH_BIN",
    "SECRETOPS_PROVIDER_TIMEOUT_SECONDS",
    "SECRETOPS_PLAN_DIR",
    "SECRETOPS_AUDIT_PATH",
    "SECRETOPS_POLICY_PATH",
)
# Fixed by the installed layout; --init writes them.
LAYOUT_DEFAULTS = {
    "SECRETOPS_BIN": "/opt/secretops/bin/secretops",
    "SECRETOPS_INFISICAL_PATH": "/",
    "SECRETOPS_PLAN_DIR": "/opt/secretops/plans",
    "SECRETOPS_AUDIT_PATH": "/opt/secretops/audit/secretops.jsonl",
    "SECRETOPS_POLICY_PATH": "/opt/secretops/policy.yaml",
}
LINE = re.compile(r"(?P<key>[A-Z][A-Z0-9_]*)=(?P<raw>.*)")
PROJECT_ID = re.compile(r"[A-Za-z0-9_-]+")
VAULT_PATH = re.compile(r"/[A-Za-z0-9_./-]*")


class ConfigError(Exception):
    """Safe to print: fixed text, line numbers, and key names only."""


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        match = LINE.fullmatch(line)
        if not match or match["key"] not in KNOWN_KEYS:
            raise ConfigError(f"broker.env line {number}: unsupported line")
        if match["key"] in values:
            raise ConfigError(f"broker.env line {number}: duplicate {match['key']}")
        try:
            parts = shlex.split(match["raw"])
        except ValueError:
            parts = None
        # Only the canonical form this module writes is accepted, so a value
        # read here means exactly what the shell will see when sourcing it.
        if parts is None or len(parts) > 1:
            raise ConfigError(f"broker.env line {number}: unsupported value syntax")
        value = parts[0] if parts else ""
        if shlex.quote(value) != match["raw"]:
            raise ConfigError(f"broker.env line {number}: unsupported value syntax")
        values[match["key"]] = value
    return values


def serialize(values: dict[str, str]) -> str:
    lines = []
    for key in KNOWN_KEYS:
        if key in values:
            value = values[key]
            if any(char in value for char in "\n\r\0"):
                raise ConfigError(f"{key}: value must be a single line")
            lines.append(f"{key}={shlex.quote(value)}")
    return "\n".join(lines) + "\n"


def validate_domain(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or any(char.isspace() for char in value)
    ):
        raise ConfigError("vault URL must be http(s)://host[:port][/path] without credentials")
    return value.rstrip("/")


def is_unencrypted_remote(domain: str) -> bool:
    parts = urlsplit(domain)
    if parts.scheme != "http":
        return False
    host = parts.hostname or ""
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def validate_project_id(value: str) -> str:
    if not PROJECT_ID.fullmatch(value):
        raise ConfigError("project ID may contain only letters, digits, '-' and '_'")
    return value


def validate_vault_path(value: str) -> str:
    if not VAULT_PATH.fullmatch(value) or "//" in value or "/../" in value + "/":
        raise ConfigError("vault folder path must be absolute, for example / or /app")
    return value


def validate_token(value: str) -> str:
    if not value or any(char.isspace() or char == "\0" for char in value):
        raise ConfigError("token must be one non-empty line without spaces; nothing was changed")
    return value


def validate_executable(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ConfigError(f"{value} is not an absolute path to an executable file")
    return value


def read_env(path: Path, owner_uid: int) -> dict[str, str]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ConfigError(f"{path} does not exist; run the installer first") from None
    except OSError:
        raise ConfigError(f"{path} cannot be opened (symlinks are refused)") from None
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077:
            raise ConfigError(f"{path} must be a regular file owned by the broker user, mode 0600")
        try:
            return parse(handle.read())
        except UnicodeDecodeError:
            raise ConfigError(f"{path} is not valid UTF-8") from None


def write_env(path: Path, values: dict[str, str], owner_uid: int, owner_gid: int) -> None:
    content = serialize(values)
    fd, temp_name = tempfile.mkstemp(prefix=".broker.env.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            os.fchown(handle.fileno(), owner_uid, owner_gid)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp_name is not None:
            os.unlink(temp_name)


def hidden_input(prompt: str) -> str:
    """Read the token with echo off, or refuse before reading anything.

    getpass falls back to visible input (with a GetPassWarning) when it
    cannot turn echo off; that warning is raised as an error here, before
    the fallback reads a single character.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ConfigError("hidden input is not available on this terminal; nothing was changed") from None
    if value:
        tell_terminal(input_received_line(value))
    return value


def tell_terminal(line: str) -> None:
    """Print one line where getpass shows its prompt: /dev/tty, else stderr."""
    try:
        fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        print(line, file=sys.stderr, flush=True)
        return
    with os.fdopen(fd, "w", encoding="utf-8") as tty:
        tty.write(line + "\n")


def ask(prompt: str, current: str, validate: Callable[[str], str], read: Callable[[str], str]) -> str:
    shown = f" [{current}]" if current else ""
    answer = read(f"{prompt}{shown}: ").strip() or current
    if not answer:
        raise ConfigError(f"{prompt} is required; nothing was changed")
    return validate(answer)


def configure(
    values: dict[str, str],
    *,
    token_only: bool,
    read: Callable[[str], str] = input,
    read_hidden: Callable[[str], str] = hidden_input,
) -> dict[str, str]:
    updated = dict(values)
    if not token_only:
        updated["INFISICAL_DOMAIN"] = ask(
            "Vault URL", values.get("INFISICAL_DOMAIN", ""), validate_domain, read
        )
        updated["SECRETOPS_INFISICAL_PROJECT_ID"] = ask(
            "Project ID", values.get("SECRETOPS_INFISICAL_PROJECT_ID", ""), validate_project_id, read
        )
        updated["SECRETOPS_INFISICAL_PATH"] = ask(
            "Vault folder path", values.get("SECRETOPS_INFISICAL_PATH", "/"), validate_vault_path, read
        )
    has_token = bool(values.get(TOKEN_KEY))
    hint = " (hidden; Enter keeps the current token)" if has_token and not token_only else " (hidden)"
    token = read_hidden(f"Machine identity access token{hint}: ")
    if token or token_only or not has_token:
        updated[TOKEN_KEY] = validate_token(token)
    return updated


def validate_env_name(value: str) -> str:
    if not ENV_NAME.fullmatch(value):
        raise ConfigError(
            "environment name must start with a letter or digit and contain only letters, digits, '.', '-' and '_'"
        )
    return value


def check_command(env: str) -> list[str]:
    return [BROKER_COMMAND, "check-vault-connection", "--env", env]


def check_timeout(values: dict[str, str]) -> int:
    raw = values.get("SECRETOPS_PROVIDER_TIMEOUT_SECONDS", "")
    provider = int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_PROVIDER_TIMEOUT
    return provider + PROBE_TIMEOUT + CHECK_MARGIN_SECONDS


def run_check(
    account: pwd.struct_passwd,
    env: str,
    timeout: int = DEFAULT_PROVIDER_TIMEOUT + PROBE_TIMEOUT + CHECK_MARGIN_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    """Run the vault check as the broker user and print its output.

    The broker wrapper loads broker.env itself, so neither the token nor any
    other setting is passed here, in the arguments or the environment. The
    child runs in a new session without root's terminal (no stdin, output
    through a pipe), so broker-user code cannot write to or inject input
    into the terminal. Returns the check's exit status, 1 when it could not
    run or timed out, CHECK_INTERRUPTED on Ctrl+C.
    """
    print(f"Checking the vault connection for environment {env} ...", flush=True)
    try:
        result = runner(
            check_command(env),
            user=account.pw_uid,
            group=account.pw_gid,
            extra_groups=[],
            env={"PATH": CHECK_PATH, "HOME": account.pw_dir, "LANG": "C.UTF-8"},
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"secretops-configure: the vault check did not finish within {timeout} seconds", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print(f"secretops-configure: the vault check could not start {BROKER_COMMAND}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nsecretops-configure: the vault check was interrupted", file=sys.stderr)
        return CHECK_INTERRUPTED
    sys.stdout.write((result.stdout or b"").decode("utf-8", errors="replace"))
    sys.stdout.flush()
    return result.returncode


def init_values(domain: str, infisical_bin: str, gh_bin: str | None) -> dict[str, str]:
    values = dict(LAYOUT_DEFAULTS)
    values["INFISICAL_DOMAIN"] = validate_domain(domain)
    values["SECRETOPS_INFISICAL_BIN"] = validate_executable(infisical_bin)
    if gh_bin:
        values["SECRETOPS_GH_BIN"] = validate_executable(gh_bin)
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secretops-configure", description=__doc__.split("\n\n")[0])
    parser.add_argument("--token-only", action="store_true", help="replace only the access token")
    parser.add_argument("--check", action="store_true",
                        help="only check the saved configuration against the vault; changes nothing")
    parser.add_argument("--env", default=DEFAULT_CHECK_ENV,
                        help="vault environment for the check (default: dev)")
    parser.add_argument("--init", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--domain", help=argparse.SUPPRESS)
    parser.add_argument("--infisical-bin", help=argparse.SUPPRESS)
    parser.add_argument("--gh-bin", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise ConfigError("run with sudo from your own terminal, not from an agent session")
        account = pwd.getpwnam(BROKER_USER)
        env = validate_env_name(args.env)
        if args.check:
            if args.token_only or args.init:
                raise ConfigError("--check cannot be combined with --token-only or --init")
            return run_check(account, env, check_timeout(read_env(BROKER_ENV, account.pw_uid)))
        if args.init:
            if BROKER_ENV.exists() or BROKER_ENV.is_symlink():
                raise ConfigError(f"{BROKER_ENV} already exists")
            if not args.domain or not args.infisical_bin:
                raise ConfigError("--init needs --domain and --infisical-bin")
            write_env(BROKER_ENV, init_values(args.domain, args.infisical_bin, args.gh_bin),
                      account.pw_uid, account.pw_gid)
            return 0
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ConfigError("an interactive terminal is required; do not pipe credentials")
        values = read_env(BROKER_ENV, account.pw_uid)
        updated = configure(values, token_only=args.token_only)
        write_env(BROKER_ENV, updated, account.pw_uid, account.pw_gid)
    except ConfigError as error:
        print(f"secretops-configure: {error}", file=sys.stderr)
        return 1
    except KeyError:
        print(f"secretops-configure: broker user {BROKER_USER} does not exist", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nsecretops-configure: cancelled; nothing was changed", file=sys.stderr)
        return 2
    print("Broker configuration saved. No credential was displayed.")
    if is_unencrypted_remote(updated["INFISICAL_DOMAIN"]):
        print("WARNING: the vault URL uses unencrypted HTTP to a non-loopback host.")
    # The exit status reports the save; a failed or interrupted check keeps it.
    code = run_check(account, env, check_timeout(updated))
    if code != 0:
        again = "sudo secretops-configure --check" + (f" --env {env}" if env != DEFAULT_CHECK_ENV else "")
        if code == CHECK_INTERRUPTED:
            print("The configuration was saved; the vault check was interrupted.")
        else:
            print("The configuration was saved, but the vault check failed. Fix the cause above,")
        print(f"then check again: {again}" if code != CHECK_INTERRUPTED else f"Check it with: {again}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
