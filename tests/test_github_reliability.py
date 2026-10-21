"""GitHub target reliability: bounded calls, write outcomes, partial results.

Adapter tests use the Popen factory doubles; process-level tests run the CLI
with fake gh and Infisical executables. Exit codes: 0 success, 1 failure
with known outcomes, 3 when any write outcome is unknown.
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from fake_process import FakeFactory, FakeProcess
from test_plans import (
    TESTS_DIR,
    generate_plan,
    import_file_plan,
    read_plan,
    run_secretops,
    sync_many_plan,
    verify_many_plan,
    write_plan,
)

from secretops.targets.github_cli import (
    GitHubCliEnvSecretTarget,
    GitHubConfigurationError,
    GitHubInterruptedError,
    GitHubLaunchError,
    GitHubProviderError,
    GitHubResponseError,
    GitHubTimeoutError,
    GitHubWriteOutcome,
)

VALUES = {
    "SOURCE_ONE": "sync-value-one",
    "SOURCE_TWO": "sync-value-two",
    "SOURCE_THREE": "sync-value-three",
    "SOURCE_FOUR": "sync-value-four",
}
PARTIAL_OUTPUT = "partial output"


def target(factory, environ=None, binary="/usr/bin/gh"):
    return GitHubCliEnvSecretTarget(
        owner="example-owner",
        repo="secretops",
        environment="dev",
        popen_factory=factory,
        binary=binary,
        environ=environ if environ is not None else {"HOME": "/home/broker"},
    )


# Timeouts


def test_every_call_receives_the_configured_timeout():
    factory = FakeFactory(FakeProcess(stdout="NAME\t2026-01-01\n"))
    github = target(factory, {"SECRETOPS_PROVIDER_TIMEOUT_SECONDS": "7"})

    github.set_secret("NAME", "value")
    github.list_secret_names()

    assert factory.process.timeouts == [7, 7]


def test_missing_timeout_uses_the_default():
    factory = FakeFactory()

    target(factory).set_secret("NAME", "value")

    assert factory.process.timeouts == [60]


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "1.5"])
def test_invalid_timeout_stops_before_gh(raw):
    factory = FakeFactory()

    with pytest.raises(GitHubConfigurationError, match="SECRETOPS_PROVIDER_TIMEOUT_SECONDS"):
        target(factory, {"SECRETOPS_PROVIDER_TIMEOUT_SECONDS": raw})
    assert factory.calls == []


def test_timeout_is_not_passed_to_gh():
    factory = FakeFactory()

    target(factory, {"SECRETOPS_PROVIDER_TIMEOUT_SECONDS": "7"}).set_secret("NAME", "value")

    assert "SECRETOPS_PROVIDER_TIMEOUT_SECONDS" not in factory.calls[0][1]["env"]


# Writes: launch boundary and outcomes


def test_acknowledged_write_sends_the_value_only_on_stdin():
    factory = FakeFactory()

    outcome = target(factory).set_secret("NAME", "secret-on-stdin")

    assert outcome is GitHubWriteOutcome.ACKNOWLEDGED
    command, kwargs = factory.calls[0]
    assert "secret-on-stdin" not in " ".join(command)
    assert "secret-on-stdin" not in json.dumps(kwargs["env"])
    assert factory.process.inputs == ["secret-on-stdin"]


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(), OSError()])
def test_launch_failure_means_nothing_was_sent(error):
    factory = FakeFactory(launch_error=error)

    with pytest.raises(GitHubLaunchError, match="gh could not be started"):
        target(factory).set_secret("NAME", "value")
    assert len(factory.calls) == 1


@pytest.mark.parametrize(
    "process",
    [
        FakeProcess(returncode=1),
        FakeProcess(returncode=-9),
        FakeProcess(hang=True),
        FakeProcess(communicate_error=BrokenPipeError()),
        FakeProcess(communicate_error=RuntimeError("unexpected")),
    ],
    ids=["nonzero", "signal", "timeout", "broken-pipe-after-launch", "unexpected-after-launch"],
)
def test_anything_after_launch_but_success_is_unknown(process):
    factory = FakeFactory(process)

    outcome = target(factory).set_secret("NAME", "value")

    assert outcome is GitHubWriteOutcome.UNKNOWN
    assert len(factory.calls) == 1  # no automatic second invocation


def test_timeout_kills_the_process():
    process = FakeProcess(hang=True)

    target(FakeFactory(process)).set_secret("NAME", "value")

    assert process.killed is True


# Metadata


def test_names_are_listed_including_valid_github_names_outside_project_pattern():
    factory = FakeFactory(FakeProcess(stdout="EXPECTED\t2026-01-01\n_FOO\t2026-01-01T10:00:00Z\nlower_name\t2026-01-01\n\n"))

    assert target(factory).list_secret_names() == ["EXPECTED", "_FOO", "lower_name"]


def test_empty_listing_is_a_valid_answer():
    assert target(FakeFactory(FakeProcess(stdout=""))).list_secret_names() == []


@pytest.mark.parametrize(
    "process,error",
    [
        (FakeProcess(returncode=1), GitHubProviderError),
        (FakeProcess(hang=True), GitHubTimeoutError),
        (FakeProcess(communicate_error=OSError()), GitHubProviderError),
        (FakeProcess(stdout="BAD-ROW-SENSITIVE-TEXT\t2026-01-01\n"), GitHubResponseError),
        (FakeProcess(stdout="1STARTS_WITH_DIGIT\t2026-01-01\n"), GitHubResponseError),
        (FakeProcess(stdout="NAME_ONLY_SENSITIVE\n"), GitHubResponseError),
        (FakeProcess(stdout="NAME\tUpdated 2026-01-01\n"), GitHubResponseError),
        (FakeProcess(stdout="NAME\t2026-01-01\tEXTRA\n"), GitHubResponseError),
        (FakeProcess(stdout=" NAME\t2026-01-01\n"), GitHubResponseError),
    ],
    ids=["nonzero", "timeout", "communication", "malformed", "digit-first", "name-only",
         "tty-format", "extra-field", "leading-space"],
)
def test_metadata_failures_raise_instead_of_returning_names(process, error):
    with pytest.raises(error) as raised:
        target(FakeFactory(process)).list_secret_names()
    assert "SENSITIVE" not in str(raised.value)


def test_metadata_launch_failure_raises():
    with pytest.raises(GitHubLaunchError):
        target(FakeFactory(launch_error=FileNotFoundError())).list_secret_names()


# A real subprocess: timeout cleanup is bounded even when a child keeps the pipes open


def test_real_timeout_kills_gh_and_returns_in_bounded_time(tmp_path):
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f"echo {PARTIAL_OUTPUT}\n"
        "sleep 30 &\n"  # inherits stdout/stderr
        "sleep 30\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)
    github = target(subprocess.Popen, {"SECRETOPS_PROVIDER_TIMEOUT_SECONDS": "1"}, binary=str(gh))

    started = time.monotonic()
    outcome = github.set_secret("NAME", "value")

    assert outcome is GitHubWriteOutcome.UNKNOWN
    assert time.monotonic() - started < 15


# Process level: fake gh driven by target-name behaviour


def write_scripted_gh(tmp_path, behaviour):
    """gh fake: per target name, 'ok' (exit 0), 'fail' (exit 1), or
    'break-after' (exit 0, then make gh unlaunchable). Records calls."""
    gh = tmp_path / "gh"
    record = tmp_path / "gh-calls.json"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"record = {str(record)!r}\n"
        f"behaviour = {behaviour!r}\n"
        "name = sys.argv[3]\n"
        "stdin = sys.stdin.read()\n"
        "calls = json.load(open(record)) if os.path.exists(record) else []\n"
        "calls.append({'name': name, 'stdin': stdin, 'argv': sys.argv[1:]})\n"
        "json.dump(calls, open(record, 'w'))\n"
        "action = behaviour.get(name, 'ok')\n"
        "if action == 'break-after':\n"
        "    open(sys.argv[0], 'w').write('#!/nonexistent/interpreter\\n')\n"
        f"print({PARTIAL_OUTPUT!r})\n"
        "raise SystemExit(1 if action == 'fail' else 0)\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)
    return record


def write_vault(tmp_path, values):
    fake = tmp_path / "infisical"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "child_env = os.environ.copy()\n"
        f"child_env.update({dict(values)!r})\n"
        "raise SystemExit(subprocess.run(command, env=child_env).returncode)\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)


def batch_plan(plan_id, mappings):
    plan = sync_many_plan(plan_id, status="approved")
    plan["secret_mappings"] = [{"source": s, "target": t} for s, t in mappings]
    return plan


MAPPINGS = [
    ("SOURCE_ONE", "TARGET_ONE"),
    ("SOURCE_TWO", "TARGET_TWO"),
    ("SOURCE_THREE", "TARGET_THREE"),
    ("SOURCE_FOUR", "TARGET_FOUR"),
]


def run_batch(tmp_path, behaviour, *, values=VALUES, mappings=MAPPINGS, audit_path=None):
    plan_dir = tmp_path / "plans"
    audit_path = audit_path or tmp_path / "audit.jsonl"
    record = write_scripted_gh(tmp_path, behaviour)
    write_vault(tmp_path, values)
    plan_path = write_plan(plan_dir, batch_plan("batch", mappings))
    result = run_secretops(
        "execute-plan", "--id", "batch",
        audit_path=audit_path, plan_dir=plan_dir, path_prefix=tmp_path,
    )
    calls = json.loads(record.read_text()) if record.exists() else []
    return result, plan_path, audit_path, calls


def audit_events(audit_path):
    if not audit_path.exists() or audit_path.is_dir():
        return []
    return [json.loads(line) for line in audit_path.read_text().splitlines()]


def assert_no_values(result, *paths):
    for value in [*VALUES.values(), PARTIAL_OUTPUT]:
        assert value not in result.stdout and value not in result.stderr
        for path in paths:
            if path.exists() and path.is_file():
                assert value not in path.read_text()


def test_fully_acknowledged_batch_exits_0(tmp_path):
    result, plan_path, audit_path, calls = run_batch(tmp_path, {})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK GitHub writes acknowledged: 4" in result.stdout
    assert [call["stdin"] for call in calls] == list(VALUES.values())
    assert read_plan(plan_path)["status"] == "executed"
    summary = audit_events(audit_path)[-1]
    assert summary["step"] == "summary" and summary["status"] == "success"
    assert [item["target"] for item in summary["acknowledged"]] == [t for _, t in MAPPINGS]
    assert_no_values(result, plan_path, audit_path)


def test_unknown_write_stops_the_batch_and_exits_3(tmp_path):
    result, plan_path, audit_path, calls = run_batch(tmp_path, {"TARGET_TWO": "fail"})

    assert result.returncode == 3
    assert [call["name"] for call in calls] == ["TARGET_ONE", "TARGET_TWO"]
    out = result.stdout
    assert "Sync stopped." in out
    for line in ("Acknowledged: 1", "Unknown: 1", "Not written: 0", "Not attempted: 2"):
        assert line in out
    assert "Do not retry automatically" in out
    assert "acknowledged writes were not rolled back" in out
    assert read_plan(plan_path)["status"] == "failed"
    events = audit_events(audit_path)
    mapping_events = [e for e in events if e["step"] == "mapping"]
    assert [(e["mapping_index"], e["outcome"]) for e in mapping_events] == [(0, "acknowledged"), (1, "unknown")]
    summary = events[-1]
    assert summary["status"] == "outcome_unknown"
    assert [i["index"] for i in summary["acknowledged"]] == [0]
    assert [i["index"] for i in summary["unknown"]] == [1]
    assert [i["index"] for i in summary["not_attempted"]] == [2, 3]
    assert summary["not_written"] == []
    assert summary["target_repo"] == "secretops"
    assert_no_values(result, plan_path, audit_path)


def test_missing_source_after_acknowledged_prefix_exits_1(tmp_path):
    values = {k: v for k, v in VALUES.items() if k != "SOURCE_TWO"}

    result, plan_path, audit_path, calls = run_batch(tmp_path, {}, values=values)

    assert result.returncode == 1
    assert [call["name"] for call in calls] == ["TARGET_ONE"]
    assert "Not written: 1" in result.stdout and "Not attempted: 2" in result.stdout
    assert "TARGET_TWO: source secret is missing" in result.stdout
    summary = audit_events(audit_path)[-1]
    assert summary["status"] == "failure"
    assert [i["index"] for i in summary["not_written"]] == [1]
    assert_no_values(result, plan_path, audit_path)


def test_launch_failure_after_acknowledged_prefix_exits_1(tmp_path):
    result, _, audit_path, calls = run_batch(tmp_path, {"TARGET_ONE": "break-after"})

    assert result.returncode == 1
    assert [call["name"] for call in calls] == ["TARGET_ONE"]
    assert "Acknowledged: 1" in result.stdout and "Not written: 1" in result.stdout
    assert "TARGET_TWO: gh could not be started" in result.stdout
    failed = [e for e in audit_events(audit_path) if e.get("failure")]
    assert failed[0]["failure"] == "launch_failed"


def test_failure_before_any_write_exits_1(tmp_path):
    result, plan_path, _, calls = run_batch(tmp_path, {}, values={})

    assert result.returncode == 1
    assert calls == []
    assert "Acknowledged: 0" in result.stdout and "Not written: 1" in result.stdout
    assert "acknowledged writes were not rolled back" not in result.stdout
    assert read_plan(plan_path)["status"] == "failed"


def test_repeated_names_are_identified_by_index(tmp_path):
    mappings = [("SOURCE_ONE", "TARGET_ONE"), ("SOURCE_ONE", "TARGET_ONE")]

    result, _, audit_path, _ = run_batch(tmp_path, {}, mappings=mappings)

    assert result.returncode == 0
    summary = audit_events(audit_path)[-1]
    assert [i["index"] for i in summary["acknowledged"]] == [0, 1]


def test_single_write_unknown_exits_3(tmp_path):
    plan_dir = tmp_path / "plans"
    write_scripted_gh(tmp_path, {"TARGET_SECRET": "fail"})
    write_vault(tmp_path, {"SOURCE_ONE": VALUES["SOURCE_ONE"]})
    plan = sync_many_plan("single", status="approved")
    plan.update(action="github.env_secret.write", secret_name="SOURCE_ONE")
    plan.pop("secret_mappings")
    plan["target"]["target_secret_name"] = "TARGET_SECRET"
    plan_path = write_plan(plan_dir, plan)

    result = run_secretops(
        "execute-plan", "--id", "single",
        audit_path=tmp_path / "audit.jsonl", plan_dir=plan_dir, path_prefix=tmp_path,
    )

    assert result.returncode == 3
    assert "GitHub write outcome unknown: TARGET_SECRET" in result.stdout
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_values(result, plan_path)


# Audit failures do not hide known progress


def test_audit_failure_after_acknowledged_write_stops_and_reports(tmp_path):
    audit_dir = tmp_path / "audit-is-a-directory"
    audit_dir.mkdir()

    result, plan_path, _, calls = run_batch(tmp_path, {}, audit_path=audit_dir)

    assert result.returncode == 1
    assert [call["name"] for call in calls] == ["TARGET_ONE"]
    assert "Acknowledged: 1" in result.stdout and "Not attempted: 3" in result.stdout
    assert "the audit record could not be completed" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_values(result, plan_path)


def test_audit_failure_while_recording_unknown_exits_3(tmp_path):
    audit_dir = tmp_path / "audit-is-a-directory"
    audit_dir.mkdir()

    result, _, _, _ = run_batch(tmp_path, {"TARGET_ONE": "fail"}, audit_path=audit_dir)

    assert result.returncode == 3
    assert "Unknown: 1" in result.stdout
    assert "the audit record could not be completed" in result.stdout


# verify_many: metadata only


def write_listing_gh(tmp_path, stdout, exit_code=0):
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write({stdout!r})\nraise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)


def run_verify(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, verify_many_plan("verify", status="planned"))
    result = run_secretops(
        "execute-plan", "--id", "verify",
        audit_path=tmp_path / "audit.jsonl", plan_dir=plan_dir, path_prefix=tmp_path,
    )
    return result, plan_path


EXPECTED_ROWS = "".join(
    f"{name}\t2026-01-01\n"
    for name in ("BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY", "BACKUP_S3_BUCKET", "BACKUP_AWS_REGION")
)


def test_unrelated_github_names_do_not_fail_or_appear(tmp_path):
    write_listing_gh(tmp_path, "_UNRELATED_PRIVATE_NAME\t2026-01-01\n" + EXPECTED_ROWS)

    result, plan_path = run_verify(tmp_path)

    assert result.returncode == 0, result.stdout
    assert "_UNRELATED_PRIVATE_NAME" not in result.stdout
    assert read_plan(plan_path)["status"] == "executed"


def test_malformed_listing_fails_without_printing_it(tmp_path):
    write_listing_gh(tmp_path, EXPECTED_ROWS + "not-a-name ROW_CONTENT\n")

    result, plan_path = run_verify(tmp_path)

    assert result.returncode == 1
    assert "unexpected gh secret list output" in result.stdout
    assert "ROW_CONTENT" not in result.stdout and "not-a-name" not in result.stdout
    assert read_plan(plan_path)["status"] == "failed"


def test_listing_error_is_not_an_empty_environment(tmp_path):
    write_listing_gh(tmp_path, "", exit_code=1)

    result, plan_path = run_verify(tmp_path)

    assert result.returncode == 1
    assert "verification failed" in result.stdout
    assert "missing names" not in result.stdout
    assert read_plan(plan_path)["status"] == "failed"


def test_empty_listing_reports_missing_names(tmp_path):
    write_listing_gh(tmp_path, "")

    result, _ = run_verify(tmp_path)

    assert result.returncode == 1
    assert "missing names: BACKUP_AWS_ACCESS_KEY_ID" in result.stdout


# Vault unknown outcomes use the same exit code


def write_unconfirmed_infisical(tmp_path):
    # Every `secrets set` "succeeds" but the read-back never sees the value.
    fake = tmp_path / "infisical"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if 'run' in args:\n"
        "    raise SystemExit(subprocess.run(args[args.index('--') + 1:]).returncode)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)


def test_unknown_vault_generate_exits_3(tmp_path):
    write_unconfirmed_infisical(tmp_path)
    plan_dir = tmp_path / "plans"
    write_plan(plan_dir, generate_plan("gen", status="approved"))

    result = run_secretops(
        "execute-plan", "--id", "gen",
        audit_path=tmp_path / "audit.jsonl", plan_dir=plan_dir, path_prefix=tmp_path,
    )

    assert result.returncode == 3
    assert "write outcome unknown" in result.stdout


def write_import_source(tmp_path, content):
    source = tmp_path / "import.env"
    source.write_text(content, encoding="utf-8")
    source.chmod(0o600)
    return source


def test_unknown_partial_import_exits_3(tmp_path):
    write_unconfirmed_infisical(tmp_path)
    source = write_import_source(tmp_path, "BACKUP_AWS_ACCESS_KEY_ID=import-value\n")
    plan_dir = tmp_path / "plans"
    write_plan(plan_dir, import_file_plan("imp", status="approved", source_file=str(source)))

    result = run_secretops(
        "execute-plan", "--id", "imp",
        audit_path=tmp_path / "audit.jsonl", plan_dir=plan_dir, path_prefix=tmp_path,
    )

    assert result.returncode == 3
    assert "write outcome unknown" in result.stdout


def test_partial_import_failing_before_the_current_write_exits_1(tmp_path):
    # The second value is empty: refused before the CLI starts.
    fake = tmp_path / "infisical"
    fake.write_text(
        f"#!{sys.executable}\n"
        f"import sys; sys.path.insert(0, {TESTS_DIR!r})\n"
        "import json, os, subprocess\n"
        "from fake_infisical import parse_set_args\n"
        f"store = {str(tmp_path / 'store.json')!r}\n"
        "stored = json.load(open(store)) if os.path.exists(store) else {}\n"
        "args = sys.argv[1:]\n"
        "if 'run' in args:\n"
        "    env = dict(os.environ, **stored)\n"
        "    raise SystemExit(subprocess.run(args[args.index('--') + 1:], env=env).returncode)\n"
        "new, _ = parse_set_args(args[args.index('set') + 1:])\n"
        "stored.update(new); json.dump(stored, open(store, 'w'))\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    source = write_import_source(
        tmp_path, "BACKUP_AWS_ACCESS_KEY_ID=first-value\nBACKUP_AWS_SECRET_ACCESS_KEY=\n"
    )
    plan_dir = tmp_path / "plans"
    write_plan(
        plan_dir,
        import_file_plan(
            "imp", status="approved", source_file=str(source),
            secret_mappings=[{"name": "BACKUP_AWS_ACCESS_KEY_ID"}, {"name": "BACKUP_AWS_SECRET_ACCESS_KEY"}],
        ),
    )

    result = run_secretops(
        "execute-plan", "--id", "imp",
        audit_path=tmp_path / "audit.jsonl", plan_dir=plan_dir, path_prefix=tmp_path,
    )

    assert result.returncode == 1, result.stdout
    assert "written and confirmed: BACKUP_AWS_ACCESS_KEY_ID" in result.stdout


# Review of ad90714: interruption, status persistence, listing structure


def test_interrupt_after_launch_stops_gh_and_is_unknown():
    process = FakeProcess(communicate_error=KeyboardInterrupt())

    outcome = target(FakeFactory(process)).set_secret("NAME", "value")

    assert outcome is GitHubWriteOutcome.UNKNOWN
    assert process.killed is True


def test_interrupt_during_listing_stops_gh_and_raises_a_check_error():
    process = FakeProcess(communicate_error=KeyboardInterrupt())

    with pytest.raises(GitHubInterruptedError):
        target(FakeFactory(process)).list_secret_names()
    assert process.killed is True


def test_sigint_during_a_sync_write_reports_partial_results_and_exits_3(tmp_path):
    started = tmp_path / "gh-started"
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time\n"
        "sys.stdin.read()\n"
        "if sys.argv[3] == 'TARGET_TWO':\n"
        f"    pathlib.Path({str(started)!r}).write_text('x')\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)
    write_vault(tmp_path, VALUES)
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, batch_plan("batch", MAPPINGS))
    env = dict(
        os.environ,
        SECRETOPS_PLAN_DIR=str(plan_dir),
        SECRETOPS_AUDIT_PATH=str(tmp_path / "audit.jsonl"),
        SECRETOPS_INFISICAL_BIN=str(tmp_path / "infisical"),
        SECRETOPS_GH_BIN=str(gh),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "secretops.cli", "execute-plan", "--id", "batch"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline and process.poll() is None
        time.sleep(0.05)
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 3, stdout + stderr
    assert "Acknowledged: 1" in stdout and "Unknown: 1" in stdout and "Not attempted: 2" in stdout
    assert "Traceback" not in stdout + stderr
    assert read_plan(plan_path)["status"] == "failed"
    summary = audit_events(tmp_path / "audit.jsonl")[-1]
    assert [item["index"] for item in summary["unknown"]] == [1]


def make_plan_store_read_only(plan_dir):
    plan_dir.chmod(0o500)


@pytest.fixture
def restore_permissions(tmp_path):
    yield
    (tmp_path / "plans").chmod(0o700)


def run_batch_with_read_only_plans(tmp_path, behaviour, audit_path=None):
    plan_dir = tmp_path / "plans"
    write_scripted_gh(tmp_path, behaviour)
    write_vault(tmp_path, VALUES)
    plan_path = write_plan(plan_dir, batch_plan("batch", MAPPINGS[:1]))
    make_plan_store_read_only(plan_dir)
    result = run_secretops(
        "execute-plan", "--id", "batch",
        audit_path=audit_path or tmp_path / "audit.jsonl", plan_dir=plan_dir, path_prefix=tmp_path,
    )
    return result, plan_path


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unsaved_status_after_acknowledged_write_exits_1(tmp_path, restore_permissions):
    result, plan_path = run_batch_with_read_only_plans(tmp_path, {})

    assert result.returncode == 1
    assert "OK GitHub writes acknowledged: 1" in result.stdout
    assert "the plan status could not be updated" in result.stdout
    assert "do not run this plan again" in result.stdout
    assert read_plan(plan_path)["status"] == "approved"
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_audit_and_status_failing_together_still_report(tmp_path, restore_permissions):
    audit_dir = tmp_path / "audit-is-a-directory"
    audit_dir.mkdir()

    result, _ = run_batch_with_read_only_plans(tmp_path, {}, audit_path=audit_dir)

    assert result.returncode == 1
    assert "Acknowledged: 1" in result.stdout or "OK GitHub writes acknowledged: 1" in result.stdout
    assert "the audit record could not be completed" in result.stdout
    assert "the plan status could not be updated" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert_no_values(result)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unsaved_status_with_unknown_write_exits_3(tmp_path, restore_permissions):
    result, _ = run_batch_with_read_only_plans(tmp_path, {"TARGET_ONE": "fail"})

    assert result.returncode == 3
    assert "the plan status could not be updated" in result.stdout
