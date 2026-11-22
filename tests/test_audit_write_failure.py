"""A failed audit write before any provider write stops with a fixed message.

Contract: every command except the human-entry ones (`add` and its endpoints)
prints the three lines below and exits 1 when an audit event cannot be
written. No traceback, no plan file change, no plan saved, and the result of
a check is not shown as if it had been recorded.
"""

import json

import pytest

from secretops.audit import AuditWriteError, write_audit_event
from test_github_reliability import MAPPINGS, VALUES, batch_plan, run_batch, write_scripted_gh, write_vault
from test_plan_binding import provider_calls, write_recording_gh, write_stateful_infisical
from test_plans import (
    DUMMY_SECRET_NAME,
    DUMMY_SECRET_VALUE,
    GENERATE_RECIPE,
    TEST_VAULT_DESTINATION,
    copy_many_plan,
    diff_envs_plan,
    verify_many_plan,
    GITHUB_RECIPE,
    VAULT_RECIPE,
    assert_no_secret_leak,
    generate_plan,
    github_plan,
    import_file_plan,
    read_plan,
    rotate_plan,
    run_secretops,
    vault_plan,
    write_fake_infisical,
    write_plan,
)

STOPPED_LINES = [
    "FAIL the audit record could not be written",
    "the command stopped before any vault or GitHub write; nothing was changed",
    "ask the operator to fix the audit log before running this again",
]


@pytest.fixture
def broken_audit(tmp_path):
    # A directory where the audit file should be: every append fails.
    path = tmp_path / "audit-is-a-directory"
    path.mkdir()
    return path


def assert_stopped(result):
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-3:] == STOPPED_LINES
    assert "Traceback" not in result.stdout + result.stderr
    assert result.stderr == ""
    assert DUMMY_SECRET_VALUE not in result.stdout + result.stderr


def test_write_audit_event_raises_an_os_error_subclass(broken_audit, monkeypatch):
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(broken_audit))
    with pytest.raises(AuditWriteError) as raised:
        write_audit_event(action="vault.read.check", secret_name="NAME", env="dev", status="planned")
    # Existing callers catch OSError; they must keep catching it.
    assert isinstance(raised.value, OSError)
    assert str(raised.value) == "audit log could not be written"


def test_saving_a_plan_stops_and_saves_nothing(tmp_path, broken_audit):
    plan_dir = tmp_path / "plans"

    result = run_secretops("plan", "--file", VAULT_RECIPE, "--save", audit_path=broken_audit, plan_dir=plan_dir)

    assert_stopped(result)
    assert "plan_id" not in result.stdout
    assert not plan_dir.exists() or list(plan_dir.glob("*.json")) == []


@pytest.mark.parametrize(
    "args",
    [
        ("plan", "--file", VAULT_RECIPE),
        ("plan", "--file", GENERATE_RECIPE),
        ("plan-recipe", "--file", VAULT_RECIPE),
        ("plan-github-env-secret", "--file", GITHUB_RECIPE),
        ("plan-vault-read", "--name", DUMMY_SECRET_NAME, "--env", "dev"),
        ("execute", "--file", GITHUB_RECIPE),
        ("show-plan", "--id", "no-such-plan"),
        ("approve-plan", "--id", "no-such-plan"),
        ("reject-plan", "--id", "no-such-plan"),
        ("preflight-plan", "--id", "no-such-plan"),
        ("execute-plan", "--id", "no-such-plan"),
    ],
    ids=lambda args: " ".join(args[:2]),
)
def test_commands_without_a_provider_call_stop(tmp_path, broken_audit, args):
    result = run_secretops(*args, audit_path=broken_audit, plan_dir=tmp_path / "plans")

    assert_stopped(result)


@pytest.mark.parametrize(
    "args",
    [
        ("check-vault-read", "--name", DUMMY_SECRET_NAME, "--env", "dev"),
        ("execute-recipe", "--file", VAULT_RECIPE),
        ("execute", "--file", VAULT_RECIPE),
    ],
    ids=lambda args: args[0] + " " + args[-1],
)
def test_a_check_result_that_cannot_be_recorded_is_not_shown(tmp_path, broken_audit, args):
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(*args, audit_path=broken_audit, path_prefix=fake_bin)

    assert_stopped(result)
    assert "OK" not in result.stdout


def test_connection_check_stops(tmp_path, broken_audit, monkeypatch):
    # Without a project ID the connection settings are incomplete, so the
    # check ends before its network probe and only the audit write remains.
    monkeypatch.delenv("SECRETOPS_INFISICAL_PROJECT_ID")

    result = run_secretops("check-vault-connection", audit_path=broken_audit)

    assert_stopped(result)
    assert "Vault:" not in result.stdout


def test_executing_a_read_check_plan_leaves_it_planned(tmp_path, broken_audit):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, vault_plan("read-check"))
    before = plan_path.read_bytes()
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "execute-plan", "--id", "read-check", audit_path=broken_audit, plan_dir=plan_dir, path_prefix=fake_bin
    )

    assert_stopped(result)
    assert "OK secret exists" not in result.stdout
    assert plan_path.read_bytes() == before
    assert_no_secret_leak(result, plan_path)


def test_preflight_leaves_the_plan_unchanged(tmp_path, broken_audit):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, vault_plan("read-check"))
    before = plan_path.read_bytes()
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan", "--id", "read-check", audit_path=broken_audit, plan_dir=plan_dir, path_prefix=fake_bin
    )

    assert_stopped(result)
    assert plan_path.read_bytes() == before


def test_refused_unapproved_write_leaves_the_plan_planned(tmp_path, broken_audit):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, generate_plan("not-approved"))
    before = plan_path.read_bytes()

    result = run_secretops("execute-plan", "--id", "not-approved", audit_path=broken_audit, plan_dir=plan_dir)

    assert_stopped(result)
    assert plan_path.read_bytes() == before
    assert read_plan(plan_path)["status"] == "planned"


def test_refused_expired_approval_makes_no_provider_call(tmp_path, broken_audit):
    plan_dir = tmp_path / "plans"
    plan = rotate_plan("expired", status="approved")
    plan["approved_at"] = "2020-01-01T00:00:00Z"
    plan_path = write_plan(plan_dir, plan)
    before = plan_path.read_bytes()
    # A vault CLI that records any call: none may happen.
    calls = tmp_path / "calls"
    fake = tmp_path / "bin" / "infisical"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho called >> {calls}\nexit 1\n", encoding="utf-8")
    fake.chmod(0o700)

    result = run_secretops(
        "execute-plan", "--id", "expired", audit_path=broken_audit, plan_dir=plan_dir, path_prefix=fake.parent
    )

    assert_stopped(result)
    assert plan_path.read_bytes() == before
    assert not calls.exists()


def test_a_working_audit_log_is_unaffected(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "check-vault-read", "--name", DUMMY_SECRET_NAME, "--env", "dev", audit_path=audit_path, path_prefix=fake_bin
    )

    assert result.returncode == 0
    assert STOPPED_LINES[0] not in result.stdout
    assert json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])["status"] == "ok"


def test_missing_secret_check_stops(tmp_path, broken_audit):
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "check-vault-read", "--name", "NO_SUCH_SECRET", "--env", "dev", audit_path=broken_audit, path_prefix=fake_bin
    )

    assert_stopped(result)
    assert "secret missing" not in result.stdout


@pytest.mark.parametrize(
    "plan,fake",
    [(diff_envs_plan("diff"), "vault"), (verify_many_plan("verify"), "gh")],
    ids=["diff_envs", "verify_many"],
)
def test_check_plan_results_that_cannot_be_recorded_leave_the_plan(tmp_path, broken_audit, plan, fake):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, plan)
    before = plan_path.read_bytes()
    if fake == "vault":
        fake_bin = write_fake_infisical(tmp_path)
    else:
        fake_bin, _ = write_recording_gh(tmp_path)

    result = run_secretops(
        "execute-plan", "--id", plan["plan_id"], audit_path=broken_audit, plan_dir=plan_dir, path_prefix=fake_bin
    )

    assert_stopped(result)
    assert plan_path.read_bytes() == before


def seed_vault(state, env, values):
    """Put values into the stateful fake vault at the test destination."""
    destination = TEST_VAULT_DESTINATION
    key = "|".join([destination["domain"], destination["project_id"], destination["path"], env])
    state.write_text(json.dumps({key: values}), encoding="utf-8")


# No audit, no write: the record made before the first provider write must
# succeed, or the command stops with the plan unchanged.


def vault_case(name, tmp_path):
    """(plan, environment to seed, values to seed, expected start details)."""
    if name == "generate":
        plan = generate_plan("generated", status="approved")
        return plan, "dev", {}, {"secret_names": ["GENERATED_DUMMY_PASSWORD"]}
    if name == "rotate":
        plan = rotate_plan("rotated", status="approved")
        return plan, "dev", {"GENERATED_DUMMY_PASSWORD": "old-dummy"}, {"secret_names": ["GENERATED_DUMMY_PASSWORD"]}
    if name == "import_file":
        source = tmp_path / "import.env"
        source.write_text("BACKUP_AWS_ACCESS_KEY_ID=import-dummy\n", encoding="utf-8")
        source.chmod(0o600)
        plan = import_file_plan("imported", status="approved", source_file=str(source))
        return plan, "staging", {}, {"secret_names": ["BACKUP_AWS_ACCESS_KEY_ID"]}
    plan = copy_many_plan("copied", status="approved")
    sources = [mapping["source"] for mapping in plan["secret_mappings"]]
    targets = [mapping["target"] for mapping in plan["secret_mappings"]]
    expected = {"secret_names": targets, "source_env": "dev", "source_secret_names": sources}
    return plan, "dev", {name: "copy-dummy" for name in sources}, expected


@pytest.mark.parametrize("case", ["generate", "rotate", "import_file", "copy_many"])
def test_no_vault_write_without_an_audit_record(tmp_path, broken_audit, case):
    plan, seed_env, seed, expected = vault_case(case, tmp_path)
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, plan)
    before = plan_path.read_bytes()
    bin_dir, calls, state = write_stateful_infisical(tmp_path)
    seed_vault(state, seed_env, seed)

    result = run_secretops(
        "execute-plan", "--id", plan["plan_id"], audit_path=broken_audit, plan_dir=plan_dir, path_prefix=bin_dir
    )

    assert_stopped(result)
    assert plan_path.read_bytes() == before
    assert not any("set" in call for call in provider_calls(calls))
    # The same plan passes every check and writes once the audit log works,
    # so the stop above came from the record before the write.
    audit_path = tmp_path / "audit.jsonl"
    again = run_secretops(
        "execute-plan", "--id", plan["plan_id"], audit_path=audit_path, plan_dir=plan_dir, path_prefix=bin_dir
    )
    assert again.returncode == 0, again.stdout + again.stderr
    assert any("set" in call for call in provider_calls(calls))
    audit_text = audit_path.read_text(encoding="utf-8")
    started = json.loads(audit_text.splitlines()[0])
    assert started["status"] == "started" and started["step"] == "write_started"
    assert started["plan_id"] == plan["plan_id"]
    assert started["env"] == (plan.get("target_env") or plan["env"])
    assert {key: started[key] for key in expected} == expected
    for value in ["old-dummy", "import-dummy", "copy-dummy"]:
        assert value not in audit_text


def github_case(name, tmp_path):
    """(plan, vault values, expected start details)."""
    if name == "write":
        plan = github_plan("single", status="approved", action="github.env_secret.write")
        return plan, {DUMMY_SECRET_NAME: "single-dummy"}, {
            "source_secret_names": [DUMMY_SECRET_NAME],
            "target_secret_names": [DUMMY_SECRET_NAME],
        }
    return batch_plan("batch", MAPPINGS), VALUES, {
        "source_secret_names": [source for source, _ in MAPPINGS],
        "target_secret_names": [target for _, target in MAPPINGS],
    }


@pytest.mark.parametrize("case", ["write", "sync_many"])
def test_no_github_write_without_an_audit_record(tmp_path, broken_audit, case):
    plan, values, expected = github_case(case, tmp_path)
    plan_dir = tmp_path / "plans"
    write_scripted_gh(tmp_path, {})
    write_vault(tmp_path, values)
    plan_path = write_plan(plan_dir, plan)
    before = plan_path.read_bytes()

    result = run_secretops(
        "execute-plan", "--id", plan["plan_id"], audit_path=broken_audit, plan_dir=plan_dir, path_prefix=tmp_path
    )

    assert_stopped(result)
    assert plan_path.read_bytes() == before
    assert not (tmp_path / "gh-calls.json").exists()
    audit_path = tmp_path / "audit.jsonl"
    again = run_secretops(
        "execute-plan", "--id", plan["plan_id"], audit_path=audit_path, plan_dir=plan_dir, path_prefix=tmp_path
    )
    assert again.returncode == 0, again.stdout + again.stderr
    assert (tmp_path / "gh-calls.json").exists()
    audit_text = audit_path.read_text(encoding="utf-8")
    started = json.loads(audit_text.splitlines()[0])
    assert started["step"] == "write_started" and started["env"] == plan["env"]
    assert {key: started[key] for key in expected} == expected
    for value in values.values():
        assert value not in audit_text + result.stdout + again.stdout


def test_the_stop_message_never_follows_a_write(tmp_path):
    # The audit log breaks during the first GitHub write: the run reports
    # what it wrote and never claims that nothing was changed.
    result, plan_path, _, calls = run_batch(tmp_path, {}, break_audit=True)

    assert result.returncode == 1
    assert [call["name"] for call in calls] == ["TARGET_ONE"]
    assert "the audit record could not be completed" in result.stdout
    for line in STOPPED_LINES:
        assert line not in result.stdout
    assert read_plan(plan_path)["status"] == "failed"


# Every event of a copy names its target environment.


def test_copy_events_name_the_target_environment_in_every_outcome(tmp_path):
    plan_dir = tmp_path / "plans"
    audit_path = tmp_path / "audit.jsonl"
    bin_dir, _, state = write_stateful_infisical(tmp_path)
    plan = copy_many_plan("copied", status="approved")
    sources = [mapping["source"] for mapping in plan["secret_mappings"]]

    # Safety-check failure: sources missing.
    write_plan(plan_dir, plan)
    run_secretops("preflight-plan", "--id", "copied", audit_path=audit_path, plan_dir=plan_dir, path_prefix=bin_dir)
    run_secretops("execute-plan", "--id", "copied", audit_path=audit_path, plan_dir=plan_dir, path_prefix=bin_dir)
    # Success.
    seed_vault(state, "dev", {name: "copy-dummy" for name in sources})
    write_plan(plan_dir, copy_many_plan("copied-ok", status="approved"))
    result = run_secretops(
        "execute-plan", "--id", "copied-ok", audit_path=audit_path, plan_dir=plan_dir, path_prefix=bin_dir
    )
    assert result.returncode == 0, result.stdout + result.stderr

    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert {event["status"] for event in events} >= {"failure", "started", "success"}
    assert {event["env"] for event in events} == {plan["target_env"]}
    assert {event["source_env"] for event in events} == {plan["source_env"]}


def test_a_copy_refused_for_its_production_source_names_that_source(tmp_path):
    plan_dir = tmp_path / "plans"
    audit_path = tmp_path / "audit.jsonl"
    plan = copy_many_plan("from-production", status="approved")
    plan["env"] = plan["source_env"] = "production"
    write_plan(plan_dir, plan)

    result = run_secretops("execute-plan", "--id", "from-production", audit_path=audit_path, plan_dir=plan_dir)

    assert result.returncode == 1
    [event] = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert (event["env"], event["source_env"]) == ("staging", "production")


def test_expired_approval_events_say_which_step_wrote_them(tmp_path):
    plan_dir = tmp_path / "plans"
    audit_path = tmp_path / "audit.jsonl"
    plan = rotate_plan("expired", status="approved")
    plan["approved_at"] = "2020-01-01T00:00:00Z"
    write_plan(plan_dir, plan)

    run_secretops("preflight-plan", "--id", "expired", audit_path=audit_path, plan_dir=plan_dir)
    run_secretops("execute-plan", "--id", "expired", audit_path=audit_path, plan_dir=plan_dir)

    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    expired = [event for event in events if event.get("failure") == "approval_expired"]
    assert [event["step"] for event in expired] == ["preflight", "execute"]
