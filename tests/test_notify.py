"""Notifications: tell the human through ntfy that a plan waits for them."""

import http.server
import json
import os
import socket
import threading

import pytest
from conftest import write_ca
from rich.console import Console
from test_plans import ROTATE_RECIPE, VAULT_RECIPE, run_secretops

from secretops import broker_config, notify
from secretops.broker_config import ConfigError, read_env, write_env
from secretops.plan_lifecycle import notify_human
from secretops.plans import PlanArtifact

TOKEN = "tk_synthetic-ntfy-token"
TOPIC = "secretops-test-topic"


class _Server:
    """A local HTTP server that records requests and answers with `status`."""

    def __init__(self, status=200, headers=None, hang=False):
        self.requests = []
        self.status = status
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length).decode()}
                )
                if hang:
                    threading.Event().wait(3)
                self.send_response(outer.status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/{TOPIC}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    started = []

    def start(**kwargs):
        started.append(_Server(**kwargs))
        return started[-1]

    yield start
    for item in started:
        item.close()


def _closed_port_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/{TOPIC}"


# --- settings -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://ntfy.sh/secretops-k3j29fx",
        "https://ntfy.example.com:8443/plans_1",
        "http://localhost:8080/plans",
        "http://127.0.0.1/plans",
        "http://[::1]:8080/plans",
    ],
)
def test_ntfy_url_accepted(url):
    assert notify.validate_ntfy_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://ntfy.example.com/plans",  # plain http off this machine
        "https://user:pass@ntfy.example.com/plans",
        "https://ntfy.example.com/",
        "https://ntfy.example.com",
        "https://ntfy.example.com/a/b",
        "https://ntfy.example.com/plans?x=1",
        "https://ntfy.example.com/plans#x",
        "https://ntfy.example.com/pl ans",
        "https://ntfy.example.com/plans.json",
        "https://ntfy.example.com/" + "a" * 65,
        "https://ntfy.example.com:99999/plans",
        "ftp://ntfy.example.com/plans",
        "https://ntfy.example.com/plänä",
    ],
)
def test_ntfy_url_refused_without_echoing_it(url):
    with pytest.raises(notify.NotifySettingsError) as error:
        notify.validate_ntfy_url(url)
    assert "example.com" not in str(error.value)


@pytest.mark.parametrize("token", ["", "has space", "line\nbreak", "x" * 513])
def test_ntfy_token_refused(token):
    with pytest.raises(notify.NotifySettingsError):
        notify.validate_ntfy_token(token)


def test_settings_check_the_ca_file_at_every_use(ca_dir):
    good = write_ca(ca_dir)
    environ = {notify.NTFY_URL_KEY: "https://ntfy.example.com/plans", notify.NOTIFY_CA_FILE_KEY: str(good)}
    assert notify.settings(environ) == ("https://ntfy.example.com/plans", None, str(good))
    good.chmod(0o666)
    with pytest.raises(notify.NotifySettingsError):
        notify.settings(environ)


# --- sending --------------------------------------------------------------


def test_send_posts_the_message_with_the_token(server):
    srv = server()
    environ = {notify.NTFY_URL_KEY: srv.url, notify.NTFY_TOKEN_KEY: TOKEN}
    assert notify.send("secretops: plan abc waits for approval", environ) == notify.SENT
    [request] = srv.requests
    assert request["path"] == f"/{TOPIC}"
    assert request["body"] == "secretops: plan abc waits for approval"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["Title"] == "secretops"


def test_send_without_a_token_sends_no_authorization(server):
    srv = server()
    assert notify.send("m", {notify.NTFY_URL_KEY: srv.url}) == notify.SENT
    assert "Authorization" not in srv.requests[0]["headers"]


@pytest.mark.parametrize(
    ("status", "headers", "reason"),
    [
        (403, None, notify.REFUSED),
        (401, None, notify.REFUSED),
        (500, None, notify.SERVER_ERROR),
        (302, {"Location": "http://127.0.0.1:1/elsewhere"}, notify.REDIRECTED),
    ],
)
def test_send_reports_a_fixed_reason_for_an_answer(server, status, headers, reason):
    srv = server(status=status, headers=headers)
    environ = {notify.NTFY_URL_KEY: srv.url, notify.NTFY_TOKEN_KEY: TOKEN}
    assert notify.send("m", environ) == reason
    # A redirect is not followed, so the token goes nowhere else.
    assert len(srv.requests) == 1


def test_send_reports_an_unreachable_server():
    assert notify.send("m", {notify.NTFY_URL_KEY: _closed_port_url()}) == notify.UNREACHABLE


def test_send_reports_a_timeout(server):
    srv = server(hang=True)
    assert notify.send("m", {notify.NTFY_URL_KEY: srv.url}, timeout=0.5) == notify.TIMED_OUT


def test_send_ignores_proxy_settings(server, monkeypatch):
    srv = server()
    monkeypatch.setenv("http_proxy", _closed_port_url().rsplit("/", 1)[0])
    monkeypatch.setenv("no_proxy", "")
    assert notify.send("m", {notify.NTFY_URL_KEY: srv.url}) == notify.SENT


def test_send_does_nothing_when_not_configured_and_never_raises_on_bad_settings():
    assert notify.send("m", {}) == notify.NOT_CONFIGURED
    assert notify.send("m", {notify.NTFY_URL_KEY: "http://ntfy.example.com/x"}) == notify.INVALID_SETTINGS
    environ = {notify.NTFY_URL_KEY: "http://127.0.0.1/x", notify.NTFY_TOKEN_KEY: "bad token"}
    assert notify.send("m", environ) == notify.INVALID_SETTINGS


def test_every_failure_reason_is_fixed_text():
    reasons = [notify.NOT_CONFIGURED, notify.INVALID_SETTINGS, notify.TLS_FAILED, notify.UNREACHABLE,
               notify.TIMED_OUT, notify.REDIRECTED, notify.REFUSED, notify.SERVER_ERROR, notify.FAILED]
    for reason in reasons:
        assert "http" not in reason and TOKEN not in reason


# --- after a plan is saved -----------------------------------------------


def _plan(action="vault.secret.rotate", **fields):
    values = {
        "plan_id": "a" * 32,
        "action": action,
        "secret_name": "API_KEY",
        "env": "dev",
        "status": "planned",
        "secret_value_included": False,
    }
    values.update(fields)
    return PlanArtifact(**values)


@pytest.fixture
def audit_path(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(path))
    return path


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _console():
    return Console(record=True, width=200, emoji=False)


def test_a_waiting_plan_notifies_with_its_id_only(server, audit_path):
    srv = server()
    console = _console()
    notify_human(_plan(), console, {notify.NTFY_URL_KEY: srv.url, notify.NTFY_TOKEN_KEY: TOKEN})
    assert srv.requests[0]["body"] == f"secretops: plan {'a' * 32} waits for approval"
    assert "API_KEY" not in srv.requests[0]["body"] and "dev" not in srv.requests[0]["body"]
    assert "- human notified: plan waits for approval" in console.export_text()
    [event] = _events(audit_path)
    assert event["status"] == "notified"
    assert event["channel"] == "ntfy"
    assert event["plan_id"] == "a" * 32
    assert "reason" not in event


def test_a_human_entry_plan_says_it_waits_for_entry(server, audit_path):
    srv = server()
    notify_human(_plan(action="vault.secret.add"), _console(), {notify.NTFY_URL_KEY: srv.url})
    assert srv.requests[0]["body"].endswith("waits for your entry")


def test_a_failed_notification_warns_and_audits_without_url_or_token(server, audit_path):
    srv = server(status=403)
    console = _console()
    notify_human(_plan(), console, {notify.NTFY_URL_KEY: srv.url, notify.NTFY_TOKEN_KEY: TOKEN})
    text = console.export_text()
    assert f"WARNING the human could not be notified: {notify.REFUSED}" in text
    assert "tell the human that this plan waits for approval" in text
    [event] = _events(audit_path)
    assert event["status"] == "notify_failed"
    assert event["reason"] == notify.REFUSED
    written = audit_path.read_text()
    for leaked in (TOKEN, TOPIC, "127.0.0.1"):
        assert leaked not in text and leaked not in written


def test_no_notification_when_not_configured_or_nothing_waits(server, audit_path):
    srv = server()
    console = _console()
    notify_human(_plan(), console, {})
    notify_human(_plan(action="vault.read.check"), console, {notify.NTFY_URL_KEY: srv.url})
    assert srv.requests == []
    assert console.export_text() == ""
    assert _events(audit_path) == []


def test_an_audit_failure_after_a_notification_is_reported_not_raised(server, tmp_path, monkeypatch):
    srv = server()
    blocked = tmp_path / "blocked"
    blocked.write_text("")
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(blocked / "audit.jsonl"))
    console = _console()
    notify_human(_plan(), console, {notify.NTFY_URL_KEY: srv.url})
    text = console.export_text()
    assert "- human notified" in text
    assert "WARNING the notification is missing from the audit log" in text


def test_plan_save_notifies_the_human(server, tmp_path, monkeypatch):
    srv = server()
    monkeypatch.setenv(notify.NTFY_URL_KEY, srv.url)
    monkeypatch.setenv(notify.NTFY_TOKEN_KEY, TOKEN)
    audit = tmp_path / "audit.jsonl"
    result = run_secretops("plan", "--file", ROTATE_RECIPE, "--save", audit_path=audit, plan_dir=tmp_path / "plans")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "- human notified: plan waits for approval" in result.stdout
    [request] = srv.requests
    plan_id = next(path.stem for path in (tmp_path / "plans").glob("*.json"))
    assert request["body"] == f"secretops: plan {plan_id} waits for approval"
    assert TOKEN not in result.stdout + result.stderr + audit.read_text()


def test_plan_save_keeps_the_plan_when_the_notification_fails(tmp_path, monkeypatch):
    monkeypatch.setenv(notify.NTFY_URL_KEY, _closed_port_url())
    result = run_secretops("plan", "--file", ROTATE_RECIPE, "--save", audit_path=tmp_path / "a.jsonl",
                           plan_dir=tmp_path / "plans")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING the human could not be notified: server unreachable" in result.stdout
    assert len(list((tmp_path / "plans").glob("*.json"))) == 1


def test_plan_without_save_and_a_plan_that_waits_for_nobody_do_not_notify(server, tmp_path, monkeypatch):
    srv = server()
    monkeypatch.setenv(notify.NTFY_URL_KEY, srv.url)
    assert run_secretops("plan", "--file", ROTATE_RECIPE, audit_path=tmp_path / "a.jsonl").returncode == 0
    saved = run_secretops("plan", "--file", VAULT_RECIPE, "--save", audit_path=tmp_path / "a.jsonl",
                          plan_dir=tmp_path / "plans")
    assert saved.returncode == 0, saved.stdout + saved.stderr
    assert srv.requests == []


def test_check_notify_command(server, monkeypatch, tmp_path):
    monkeypatch.delenv(notify.NTFY_URL_KEY, raising=False)
    missing = run_secretops("check-notify", audit_path=tmp_path / "a.jsonl")
    assert missing.returncode == 1 and "not configured" in missing.stdout
    srv = server()
    monkeypatch.setenv(notify.NTFY_URL_KEY, srv.url)
    sent = run_secretops("check-notify", audit_path=tmp_path / "a.jsonl")
    assert sent.returncode == 0 and "OK test notification sent" in sent.stdout
    assert srv.requests[0]["body"] == notify.TEST_MESSAGE
    monkeypatch.setenv(notify.NTFY_URL_KEY, _closed_port_url())
    failed = run_secretops("check-notify", audit_path=tmp_path / "a.jsonl")
    assert failed.returncode == 1 and "server unreachable" in failed.stdout
    assert "127.0.0.1" not in failed.stdout


# --- secretops-configure ---------------------------------------------------


def _account():
    import pwd

    return pwd.getpwuid(os.getuid())


@pytest.fixture
def root(monkeypatch, tmp_path):
    env_file = tmp_path / "broker.env"
    write_env(env_file, {"INFISICAL_TOKEN": "old"}, os.getuid(), os.getgid())
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(broker_config.pwd, "getpwnam", lambda name: _account())
    monkeypatch.setattr(broker_config, "BROKER_ENV", env_file)
    monkeypatch.setattr(broker_config, "is_interactive", lambda: True)
    monkeypatch.setattr(broker_config, "run_check", lambda *a, **k: pytest.fail("no vault check"))
    tests = []

    def fake_notify_check(account, runner=None):
        tests.append(account.pw_uid)
        return fake_notify_check.code

    fake_notify_check.code = 0
    monkeypatch.setattr(broker_config, "run_notify_check", fake_notify_check)
    return env_file, tests, fake_notify_check


def _hidden(reply):
    return lambda prompt: reply


def test_configure_sets_url_and_token_then_sends_a_test(root, monkeypatch, capsys):
    env_file, tests, _ = root
    monkeypatch.setattr(broker_config, "hidden_input", _hidden(TOKEN))
    assert broker_config.main(["--notify-ntfy", "https://ntfy.example.com/plans"]) == 0
    values = read_env(env_file, os.getuid())
    assert values[notify.NTFY_URL_KEY] == "https://ntfy.example.com/plans"
    assert values[notify.NTFY_TOKEN_KEY] == TOKEN
    assert values["INFISICAL_TOKEN"] == "old"
    assert tests == [os.getuid()]
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_configure_notify_token_rules(ca_dir):
    base = {"INFISICAL_TOKEN": "old", notify.NTFY_TOKEN_KEY: "previous"}
    with_token = broker_config.configure_notify(
        base, url="https://ntfy.example.com/plans", ca_file=None, read_hidden=_hidden(TOKEN))
    assert with_token[notify.NTFY_TOKEN_KEY] == TOKEN
    without = broker_config.configure_notify(
        base, url="https://ntfy.example.com/plans", ca_file=None, read_hidden=_hidden(""))
    assert notify.NTFY_TOKEN_KEY not in without
    with pytest.raises(ConfigError):
        broker_config.configure_notify(base, url="http://ntfy.example.com/plans", ca_file=None,
                                       read_hidden=_hidden(TOKEN))
    with pytest.raises(ConfigError):
        broker_config.configure_notify(base, url="https://ntfy.example.com/plans", ca_file=None,
                                       read_hidden=_hidden("bad token"))
    ca = write_ca(ca_dir)
    only_ca = broker_config.configure_notify(base, url=None, ca_file=str(ca),
                                             read_hidden=lambda p: pytest.fail("must not ask"))
    assert only_ca[notify.NOTIFY_CA_FILE_KEY] == str(ca)
    assert only_ca[notify.NTFY_TOKEN_KEY] == "previous"


def test_refused_settings_change_nothing(root, monkeypatch, capsys):
    env_file, tests, _ = root
    before = env_file.read_text()
    monkeypatch.setattr(broker_config, "hidden_input", lambda p: pytest.fail("must not ask"))
    assert broker_config.main(["--notify-ntfy", "http://ntfy.example.com/plans"]) == 1
    assert env_file.read_text() == before
    assert tests == []
    assert "ntfy.example.com" not in capsys.readouterr().err


def test_ntfy_url_needs_a_terminal(root, monkeypatch):
    env_file, tests, _ = root
    before = env_file.read_text()
    monkeypatch.setattr(broker_config, "is_interactive", lambda: False)
    assert broker_config.main(["--notify-ntfy", "https://ntfy.example.com/plans"]) == 1
    assert env_file.read_text() == before


def test_a_failed_test_keeps_the_save(root, monkeypatch, capsys):
    env_file, tests, fake = root
    fake.code = 1
    monkeypatch.setattr(broker_config, "hidden_input", _hidden(""))
    assert broker_config.main(["--notify-ntfy", "https://ntfy.example.com/plans"]) == 0
    assert read_env(env_file, os.getuid())[notify.NTFY_URL_KEY] == "https://ntfy.example.com/plans"
    assert "sudo secretops-configure --check-notify" in capsys.readouterr().out


def test_ca_file_alone_without_a_topic_sends_no_test(root, ca_dir, capsys):
    env_file, tests, _ = root
    ca = write_ca(ca_dir)
    assert broker_config.main(["--notify-ca-file", str(ca)]) == 0
    assert read_env(env_file, os.getuid())[notify.NOTIFY_CA_FILE_KEY] == str(ca)
    assert tests == []
    assert "--notify-ntfy URL" in capsys.readouterr().out


def test_no_notify_removes_every_notification_key(root):
    env_file, tests, _ = root
    write_env(env_file, {"INFISICAL_TOKEN": "old", notify.NTFY_URL_KEY: "https://ntfy.example.com/plans",
                         notify.NTFY_TOKEN_KEY: TOKEN, notify.NOTIFY_CA_FILE_KEY: "/etc/ca.pem"},
              os.getuid(), os.getgid())
    assert broker_config.main(["--no-notify"]) == 0
    assert read_env(env_file, os.getuid()) == {"INFISICAL_TOKEN": "old"}
    assert tests == []


def test_check_notify_only_tests(root):
    env_file, tests, fake = root
    before = env_file.read_text()
    fake.code = 1
    assert broker_config.main(["--check-notify"]) == 1
    assert tests == [os.getuid()]
    assert env_file.read_text() == before


@pytest.mark.parametrize(
    "argv",
    [
        ["--no-notify", "--notify-ntfy", "https://ntfy.example.com/plans"],
        ["--no-notify", "--check-notify"],
        ["--check-notify", "--notify-ca-file", "/etc/ca.pem"],
        ["--notify-ntfy", "https://ntfy.example.com/plans", "--check"],
        ["--notify-ntfy", "https://ntfy.example.com/plans", "--token-only"],
        ["--no-notify", "--no-ca-file"],
    ],
)
def test_notification_options_refuse_combinations(root, argv, capsys):
    env_file, tests, _ = root
    before = env_file.read_text()
    assert broker_config.main(argv) == 1
    assert env_file.read_text() == before
    assert tests == []
    assert "secretops-configure:" in capsys.readouterr().err


def test_notify_check_runs_as_the_broker_without_credentials(capsys):
    calls = []

    class Result:
        returncode = 0
        stdout = b"OK test notification sent; check that it arrived\n"

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    assert broker_config.run_notify_check(_account(), runner=runner) == 0
    [(command, kwargs)] = calls
    assert command == ["/usr/local/bin/secretops-broker", "check-notify"]
    assert set(kwargs["env"]) == {"PATH", "HOME", "LANG"}
    assert kwargs["stdin"] is not None and kwargs["start_new_session"] is True
    assert "OK test notification sent" in capsys.readouterr().out


def test_broker_env_accepts_the_notification_keys():
    values = {notify.NTFY_URL_KEY: "https://ntfy.example.com/plans", notify.NTFY_TOKEN_KEY: TOKEN,
              notify.NOTIFY_CA_FILE_KEY: "/etc/ca.pem"}
    assert broker_config.parse(broker_config.serialize(values)) == values
