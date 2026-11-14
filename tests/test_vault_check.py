"""Vault connection check: failure classification, probe, command, no leaks."""

import http.server
import json
import socket
import subprocess
import threading

import pytest
from typer.testing import CliRunner

from secretops import vault_check
from secretops.cli import app
from secretops.vault_check import CheckResult, Probe, check_vault_connection, probe_server
from secretops.vaults import (
    VaultNotFoundError,
    VaultProviderError,
    VaultTokenRefusedError,
    VaultUnreachableError,
)
from secretops.vaults.infisical_cli import InfisicalCliVault, classify_failure

MARKER = "PROVIDER-OUTPUT-MARKER-7f3a"
TOKEN = "fake-token-4b1d9e"
ENVIRON = {
    "INFISICAL_TOKEN": TOKEN,
    "INFISICAL_DOMAIN": "https://vault.example.com",
    "SECRETOPS_INFISICAL_PROJECT_ID": "project-1",
    "SECRETOPS_INFISICAL_PATH": "/",
}
UNREACHABLE = 'Get "https://vault.example.com/api/v3/secrets/raw": dial tcp: lookup vault.example.com: no such host'
REFUSED_CONNECTION = "dial tcp 127.0.0.1:8086: connect: connection refused"
TLS = "tls: failed to verify certificate: x509: certificate signed by unknown authority"
STATUS = "error: CallGetRawSecretsV3: Unsuccessful response [GET https://vault.example.com/api/v3/secrets/raw] [status-code={code}] [response={{}}]"


def failing_runner(stderr, calls=None, stdout=""):
    def run(command, **kwargs):
        if calls is not None:
            calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, stdout, stderr)

    return run


def factory(runner):
    def make(**kwargs):
        return InfisicalCliVault(runner=runner, binary="infisical", environ=ENVIRON, **kwargs)

    return make


def missing_runner(command, **kwargs):
    # A successful `infisical run` whose helper reports the name missing.
    child_env = {"PATH": kwargs["env"]["PATH"]}
    child = command[command.index("--") + 1 :]
    return subprocess.run(child, text=True, capture_output=True, env=child_env, check=False)


@pytest.mark.parametrize(
    ("stderr", "confirmed", "expected"),
    [
        (UNREACHABLE, False, VaultUnreachableError),
        (REFUSED_CONNECTION, False, VaultUnreachableError),
        (TLS, False, VaultUnreachableError),
        (STATUS.format(code=401), False, VaultTokenRefusedError),
        (STATUS.format(code=403), False, VaultNotFoundError),
        (STATUS.format(code=404), True, VaultNotFoundError),
        # A 404 can come from a server that is not the Infisical API.
        (STATUS.format(code=404), False, None),
        (STATUS.format(code=500), True, None),
        (STATUS.format(code=401) + "\n" + STATUS.format(code=403), True, None),
        (STATUS.format(code=401) + "\n" + UNREACHABLE, True, None),
        (f"error: something new {MARKER}", True, None),
        ("", True, None),
    ],
)
def test_classification(stderr, confirmed, expected):
    assert classify_failure(stderr, api_confirmed=confirmed) is expected


def test_only_stderr_is_classified():
    vault = factory(failing_runner("", stdout=STATUS.format(code=401)))()
    with pytest.raises(VaultProviderError) as error:
        vault.secret_exists("DUMMY_SECRET", "dev")
    assert type(error.value) is VaultProviderError


@pytest.mark.parametrize(
    ("stderr", "kind", "text"),
    [
        (UNREACHABLE, VaultUnreachableError, "vault not reachable at the configured URL"),
        (STATUS.format(code=401), VaultTokenRefusedError, "refused the access token"),
        (STATUS.format(code=403), VaultNotFoundError, "project, environment or folder not found"),
        ("unrecognised", VaultProviderError, "infisical CLI failed with exit status 1"),
    ],
)
def test_ordinary_operations_get_the_fixed_message(stderr, kind, text):
    vault = factory(failing_runner(f"{stderr} {MARKER} {TOKEN}"))()
    with pytest.raises(kind) as error:
        vault.secret_exists("DUMMY_SECRET", "dev")
    message = str(error.value)
    assert text in message
    assert MARKER not in message and TOKEN not in message
    assert error.value.__cause__ is None and error.value.__context__ is None


def test_failed_write_stays_unknown_whatever_the_output():
    from secretops.vaults import WriteOutcome

    vault = factory(failing_runner(STATUS.format(code=401)))()
    assert vault.write_secret("DUMMY_SECRET", "dev", "value") is WriteOutcome.UNKNOWN


@pytest.mark.parametrize(
    ("probe", "stderr", "expected"),
    [
        (Probe.CONFIRMED, UNREACHABLE, CheckResult.UNREACHABLE),
        (Probe.INCONCLUSIVE, TLS, CheckResult.UNREACHABLE),
        (Probe.CONFIRMED, STATUS.format(code=401), CheckResult.TOKEN_REFUSED),
        (Probe.INCONCLUSIVE, STATUS.format(code=403), CheckResult.NOT_FOUND),
        (Probe.CONFIRMED, STATUS.format(code=404), CheckResult.NOT_FOUND),
        (Probe.INCONCLUSIVE, STATUS.format(code=404), CheckResult.UNKNOWN),
        (Probe.CONFIRMED, f"new text {MARKER}", CheckResult.UNKNOWN),
    ],
)
def test_check_results(probe, stderr, expected):
    result, detail = check_vault_connection(
        "dev", probe=lambda domain: probe, vault_factory=factory(failing_runner(stderr))
    )
    assert result is expected
    assert MARKER not in detail and TOKEN not in detail
    if expected is CheckResult.UNKNOWN:
        assert detail == "infisical CLI failed with exit status 1"


def test_check_working_looks_up_one_synthetic_name_without_token_in_argv():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return missing_runner(command, **kwargs)

    result, _ = check_vault_connection("staging", probe=lambda domain: Probe.CONFIRMED, vault_factory=factory(run))
    assert result is CheckResult.WORKING
    (command, kwargs), = calls
    assert command[-1] == vault_check.CHECK_SECRET_NAME
    assert "--env=staging" in command and "run" in command and "set" not in command
    assert all(TOKEN not in part for part in command)
    assert kwargs["env"]["INFISICAL_TOKEN"] == TOKEN


def test_unreachable_probe_never_starts_the_cli():
    calls = []
    result, _ = check_vault_connection(
        "dev", probe=lambda domain: Probe.UNREACHABLE, vault_factory=factory(failing_runner("", calls))
    )
    assert result is CheckResult.UNREACHABLE
    assert calls == []


def test_configuration_error_is_unknown_with_its_safe_message():
    def make(**kwargs):
        return InfisicalCliVault(binary="infisical", environ={"INFISICAL_TOKEN": TOKEN}, **kwargs)

    result, detail = check_vault_connection("dev", probe=lambda domain: Probe.CONFIRMED, vault_factory=make)
    assert result is CheckResult.UNKNOWN
    assert "SECRETOPS_INFISICAL_PROJECT_ID" in detail


@pytest.mark.parametrize("result", list(CheckResult))
def test_every_result_has_fixed_text_with_a_next_step(result):
    lines = vault_check.result_lines(result, "", "dev")
    assert lines[0].startswith("OK" if result is CheckResult.WORKING else "FAIL")
    if result is not CheckResult.WORKING:
        assert any("Next:" in line for line in lines)


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b'{"message":"Ok"}'
    paths: list = []
    headers_seen: list = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).paths.append(self.path)
        type(self).headers_seen.append(dict(self.headers))
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Handler.paths, _Handler.headers_seen = [], []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, b'{"message":"Ok"}', Probe.CONFIRMED),
        (200, b"<html>not the api</html>", Probe.INCONCLUSIVE),
        (404, b"not found", Probe.INCONCLUSIVE),
    ],
)
def test_probe_answers(server, monkeypatch, status, body, expected):
    monkeypatch.setattr(_Handler, "status", status)
    monkeypatch.setattr(_Handler, "body", body)
    domain = f"http://127.0.0.1:{server.server_port}/base"
    assert probe_server(domain) is expected
    assert _Handler.paths == ["/base/api/status"]
    assert not any("authorization" in key.lower() for key in _Handler.headers_seen[0])


def test_probe_refused_connection_is_unreachable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert probe_server(f"http://127.0.0.1:{port}") is Probe.UNREACHABLE


def test_probe_ignores_proxy_variables(server, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert probe_server(f"http://127.0.0.1:{server.server_port}") is Probe.CONFIRMED


def write_fake_cli(tmp_path, stderr_text):
    """A fake infisical that prints a marker and the token it received to
    both streams, records its argv, and fails."""
    argv_file = tmp_path / "argv.json"
    fake = tmp_path / "infisical"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(argv_file)!r}, 'w').write(json.dumps(sys.argv))\n"
        "token = os.environ.get('INFISICAL_TOKEN', '')\n"
        f"sys.stdout.write({MARKER!r} + ' ' + token + '\\n')\n"
        f"sys.stderr.write({stderr_text!r} + ' ' + {MARKER!r} + ' ' + token + '\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    return fake, argv_file


@pytest.mark.parametrize(
    ("stderr_text", "expected_line", "status"),
    [
        (STATUS.format(code=401), "FAIL the vault refused the access token", "token_refused"),
        ("something unexpected", "FAIL could not tell what is wrong", "unknown"),
    ],
)
def test_command_prints_fixed_text_and_leaks_nothing(tmp_path, monkeypatch, stderr_text, expected_line, status):
    fake, argv_file = write_fake_cli(tmp_path, stderr_text)
    audit = tmp_path / "audit.jsonl"
    for key, value in ENVIRON.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SECRETOPS_INFISICAL_BIN", str(fake))
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(audit))
    monkeypatch.setattr(vault_check, "probe_server", lambda domain: Probe.CONFIRMED)

    result = CliRunner().invoke(app, ["check-vault-connection", "--env", "dev"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Vault: https://vault.example.com, project project-1, environment dev, folder /" in result.output
    assert expected_line in result.output
    assert "Provider output and secret values were not printed." in result.output
    recorded = audit.read_text(encoding="utf-8")
    assert json.loads(recorded)["result"] == status
    for text in (result.output, recorded):
        assert MARKER not in text
        assert TOKEN not in text
    assert TOKEN not in argv_file.read_text(encoding="utf-8")


def test_command_working(tmp_path, monkeypatch):
    audit = tmp_path / "audit.jsonl"
    for key, value in ENVIRON.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(audit))
    monkeypatch.setattr(vault_check, "probe_server", lambda domain: Probe.CONFIRMED)
    monkeypatch.setattr(vault_check, "InfisicalCliVault", factory(missing_runner))

    result = CliRunner().invoke(app, ["check-vault-connection"])

    assert result.exit_code == 0
    assert result.output.splitlines()[1].startswith("OK vault connection works")
    assert json.loads(audit.read_text(encoding="utf-8"))["status"] == "ok"


def test_command_refuses_invalid_environment():
    result = CliRunner().invoke(app, ["check-vault-connection", "--env", "dev;id"])
    assert result.exit_code == 1
    assert "environment name is not valid" in result.output
