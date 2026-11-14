"""execute-plan on plans that are not (or no longer) approved.

Run in process with a fake vault and GitHub adapter, so every test can show
that no provider adapter was built.
"""

import io
import json
from datetime import datetime, timedelta, timezone
from importlib import resources

import pytest
import typer
import yaml
from rich.console import Console
from test_plans import (
    DUMMY_SECRET_VALUE,
    copy_many_plan,
    diff_envs_plan,
    generate_plan,
    github_plan,
    import_file_plan,
    read_plan,
    rotate_plan,
    sync_many_plan,
    vault_plan,
    verify_many_plan,
    write_plan,
)

import secretops.plan_lifecycle
import secretops.plans
from secretops.plan_lifecycle import approval_expiry_refusal
from secretops.plans import format_timestamp
from secretops.vaults import WriteOutcome

PENDING = "the plan is not approved yet; it stays planned and nothing was done"


class FakeVault:
    atomic_create = False

    def __init__(self):
        self.values = {}

    def secret_exists(self, name, env):
        return (name, env) in self.values

    def read_secret_value(self, name, env):
        return self.values.get((name, env))

    def write_secret(self, name, env, value):
        self.values[(name, env)] = value
        return WriteOutcome.CONFIRMED


@pytest.fixture
def broker(monkeypatch, tmp_path):
    plan_dir = tmp_path / "plans"
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SECRETOPS_PLAN_DIR", str(plan_dir))
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(audit_path))
    calls = []
    vault = FakeVault()

    def fake_get_vault(destination):
        calls.append("vault")
        return vault

    def fake_github(**kwargs):
        calls.append("gh")
        raise AssertionError("no GitHub adapter may be built")

    monkeypatch.setattr(secretops.plan_lifecycle, "get_vault", fake_get_vault)
    monkeypatch.setattr(secretops.plan_lifecycle, "GitHubCliEnvSecretTarget", fake_github)

    class Broker:
        def __init__(self):
            self.plan_dir = plan_dir
            self.audit_path = audit_path
            self.calls = calls
            self.vault = vault

        def write(self, plan):
            return write_plan(plan_dir, plan)

        def policy(self, change):
            data = yaml.safe_load(
                resources.files("secretops.policies").joinpath("default.yaml").read_text("utf-8")
            )
            change(data)
            path = tmp_path / "policy.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            monkeypatch.setenv("SECRETOPS_POLICY_PATH", str(path))

        def run(self, function, *args):
            buffer = io.StringIO()
            code = 0
            try:
                function(*args, Console(file=buffer, width=200))
            except typer.Exit as error:
                code = error.exit_code
            return code, buffer.getvalue().strip().splitlines()

        def execute(self, plan_id):
            return self.run(secretops.plan_lifecycle.execute_saved_plan, plan_id)

        def approve(self, plan_id):
            return self.run(
                secretops.plan_lifecycle.transition_planned_plan, plan_id, "approved", "approved"
            )

        def audit_events(self):
            if not self.audit_path.exists():
                return []
            return [json.loads(line) for line in self.audit_path.read_text("utf-8").splitlines()]

    return Broker()


APPROVAL_PLANS = {
    "generate": (generate_plan, "vault.secret.generate plan requires approval"),
    "rotate": (rotate_plan, "vault.secret.rotate plan requires approval"),
    "import": (import_file_plan, "vault.secret.import_file plan requires approval"),
    "copy": (copy_many_plan, "vault.secret.copy_many plan requires approval"),
    "github_write": (
        lambda plan_id, status="planned": github_plan(plan_id, status, "github.env_secret.write"),
        "GitHub write plan requires approval",
    ),
    "github_sync": (sync_many_plan, "GitHub sync_many plan requires approval"),
    "github_plan": (github_plan, "GitHub plan requires approval"),
}


@pytest.mark.parametrize("kind", sorted(APPROVAL_PLANS))
def test_planned_plan_is_refused_and_left_planned(broker, kind):
    make_plan, message = APPROVAL_PLANS[kind]
    plan_path = broker.write(make_plan(f"pending-{kind}"))
    before = plan_path.read_bytes()

    code, lines = broker.execute(f"pending-{kind}")

    assert code == 1
    assert lines[0] == f"FAIL {message}"
    assert lines[1:4] == [PENDING, "no provider call was made",
                          "wait until the human has approved it; do not retry to check"]
    assert plan_path.read_bytes() == before
    assert broker.calls == []
    event = broker.audit_events()[-1]
    assert event["status"] == "failure"
    assert event["failure"] == "approval_pending"
    assert event["plan_id"] == f"pending-{kind}"


@pytest.mark.parametrize(
    "kind", ["generate", "rotate", "copy", "github_write", "github_sync"]
)
def test_per_action_approval_check_also_leaves_the_plan_planned(broker, kind):
    # With requires_approval switched off in the policy, the action's own
    # check still demands approval, with the same behaviour.
    make_plan, message = APPROVAL_PLANS[kind]
    plan = make_plan(f"inner-{kind}")
    broker.policy(lambda data: data["actions"][plan["action"]].update(requires_approval=False))
    plan_path = broker.write(plan)

    code, lines = broker.execute(f"inner-{kind}")

    assert code == 1
    assert f"FAIL {message}" in lines
    assert PENDING in lines
    assert read_plan(plan_path)["status"] == "planned"
    assert broker.calls == []


def test_refused_plan_can_still_be_approved_and_executed(broker):
    broker.write(generate_plan("later-approved"))

    assert broker.execute("later-approved")[0] == 1
    assert broker.approve("later-approved")[0] == 0
    code, lines = broker.execute("later-approved")

    assert code == 0
    assert "OK generated secret stored: GENERATED_DUMMY_PASSWORD" in lines
    assert read_plan(broker.plan_dir / "later-approved.json")["status"] == "executed"
    for line in lines:
        for value in broker.vault.values.values():
            assert value not in line


def test_failed_plan_that_needs_approval_stays_failed(broker):
    plan_path = broker.write(generate_plan("already-failed", status="failed"))

    code, lines = broker.execute("already-failed")

    assert code == 1
    assert lines[0] == "FAIL vault.secret.generate plan requires approval"
    assert PENDING not in lines
    assert read_plan(plan_path)["status"] == "failed"
    assert broker.calls == []


def test_planned_plan_refused_by_environment_policy_is_marked_failed(broker):
    plan = generate_plan("production-pending")
    plan["env"] = "production"
    plan_path = broker.write(plan)

    code, lines = broker.execute("production-pending")

    assert code == 1
    assert "production" in lines[0]
    assert PENDING not in lines
    assert read_plan(plan_path)["status"] == "failed"
    assert broker.calls == []


@pytest.mark.parametrize("status,message", [
    ("rejected", "FAIL rejected plan cannot execute"),
    ("executed", "FAIL plan already executed; save a new plan to run it again"),
])
def test_rejected_and_executed_plans_are_refused_unchanged(broker, status, message):
    plan_path = broker.write(generate_plan(f"done-{status}", status=status))
    before = plan_path.read_bytes()

    code, lines = broker.execute(f"done-{status}")

    assert code == 1
    assert lines[0] == message
    assert plan_path.read_bytes() == before
    assert broker.calls == []


def test_pending_refusal_output_and_audit_hold_no_secret_value(broker):
    broker.write(sync_many_plan("pending-leak"))

    _, lines = broker.execute("pending-leak")

    assert DUMMY_SECRET_VALUE not in "\n".join(lines)
    assert DUMMY_SECRET_VALUE not in broker.audit_path.read_text("utf-8")


# Approval expiry

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
EXPIRED_HINT = "save a new plan and ask the human to review and approve it"


def at(**delta):
    return format_timestamp(NOW + timedelta(**delta))


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(secretops.plan_lifecycle, "utc_now", lambda: NOW)
    return NOW


def approved(make_plan, plan_id, approved_at):
    plan = make_plan(plan_id, status="approved")
    plan["approved_at"] = approved_at
    return plan


ALL_EXECUTABLE = {
    **{kind: make for kind, (make, _) in APPROVAL_PLANS.items() if kind != "github_plan"},
    "read_check": vault_plan,
    "diff_envs": diff_envs_plan,
    "verify_many": verify_many_plan,
}


@pytest.mark.parametrize("kind", sorted(ALL_EXECUTABLE))
def test_expired_approval_is_refused_before_any_provider_call(broker, clock, kind):
    plan_path = broker.write(approved(ALL_EXECUTABLE[kind], f"expired-{kind}", at(hours=-25)))

    code, lines = broker.execute(f"expired-{kind}")

    assert code == 1
    assert lines[:3] == [
        "FAIL approval expired: approved more than 24 hours ago",
        "no provider call was made; the plan is now failed",
        EXPIRED_HINT,
    ]
    assert read_plan(plan_path)["status"] == "failed"
    assert broker.calls == []
    event = broker.audit_events()[-1]
    assert (event["status"], event["failure"]) == ("failure", "approval_expired")


def test_fresh_approval_executes(broker, clock):
    broker.write(approved(generate_plan, "fresh", at(hours=-23, minutes=-59)))

    code, lines = broker.execute("fresh")

    assert code == 0
    assert lines[0] == "OK generated secret stored: GENERATED_DUMMY_PASSWORD"


@pytest.mark.parametrize("age,refused", [
    (timedelta(hours=24) - timedelta(seconds=1), False),
    (timedelta(hours=24), True),
    (timedelta(0), False),
    (timedelta(seconds=-1), True),
])
def test_expiry_boundary_and_future_time(age, refused):
    plan = type("Plan", (), {"status": "approved", "approved_at": format_timestamp(NOW - age)})

    assert (approval_expiry_refusal(plan, 24, NOW) is not None) is refused


def test_approval_time_in_the_future_is_refused(broker, clock):
    plan_path = broker.write(approved(generate_plan, "future", at(minutes=5)))

    code, lines = broker.execute("future")

    assert code == 1
    assert lines[0] == "FAIL approval time is in the future (the clock may have changed)"
    assert read_plan(plan_path)["status"] == "failed"
    assert broker.calls == []


def test_plan_approved_by_an_older_version_is_refused(broker, clock):
    plan_path = broker.write(approved(generate_plan, "old-approved", None))
    assert "approved_at" not in read_plan(plan_path)

    code, lines = broker.execute("old-approved")

    assert code == 1
    assert lines[:3] == [
        "FAIL plan was approved by an older secretops version without an approval time",
        "no provider call was made; the plan is now failed",
        EXPIRED_HINT,
    ]
    assert read_plan(plan_path)["status"] == "failed"
    assert broker.calls == []


def test_planned_plan_of_an_older_version_gets_times_when_approved(broker, clock):
    plan_path = broker.write(generate_plan("old-planned"))
    assert "planned_at" not in read_plan(plan_path)

    code, lines = broker.approve("old-planned")

    assert code == 0
    assert "approved_at: 2026-11-14T12:00:00Z" in lines
    assert "approval_expires_at: 2026-11-15T12:00:00Z" in lines
    assert read_plan(plan_path)["approved_at"] == "2026-11-14T12:00:00Z"
    assert broker.execute("old-planned")[0] == 0


def test_configured_period_applies(broker, clock):
    broker.policy(lambda data: data.update(approval_valid_hours=2))
    broker.write(approved(generate_plan, "short-ok", at(hours=-1)))
    broker.write(approved(generate_plan, "short-expired", at(hours=-3)))

    assert broker.execute("short-ok")[0] == 0
    code, lines = broker.execute("short-expired")
    assert code == 1
    assert lines[0] == "FAIL approval expired: approved more than 2 hours ago"


def test_show_plan_prints_plan_and_approval_times(broker, clock):
    plan = approved(generate_plan, "show-times", at(hours=-2))
    plan["planned_at"] = at(hours=-3)
    broker.write(plan)

    _, lines = broker.run(secretops.plan_lifecycle.show_saved_plan, "show-times")

    assert lines[-4:] == [
        "planned_at: 2026-11-14T09:00:00Z",
        "approved_at: 2026-11-14T10:00:00Z",
        "approval_expires_at: 2026-11-15T10:00:00Z",
        "approval_valid: yes",
    ]


def test_show_plan_marks_expired_and_older_approvals(broker, clock):
    broker.write(approved(generate_plan, "show-expired", at(hours=-30)))
    broker.write(approved(generate_plan, "show-old", None))
    broker.write(generate_plan("show-planned"))

    _, expired = broker.run(secretops.plan_lifecycle.show_saved_plan, "show-expired")
    _, old = broker.run(secretops.plan_lifecycle.show_saved_plan, "show-old")
    _, planned = broker.run(secretops.plan_lifecycle.show_saved_plan, "show-planned")

    assert expired[-1] == "approval_valid: no; approval expired: approved more than 24 hours ago"
    assert old[-3:] == [
        "planned_at: not recorded (older version)",
        "approved_at: not recorded (older version)",
        "approval_valid: no; plan was approved by an older secretops version without an "
        "approval time",
    ]
    assert planned[-1] == "planned_at: not recorded (older version)"
    for lines in (expired, old, planned):
        assert DUMMY_SECRET_VALUE not in "\n".join(lines)


def test_new_plans_record_the_planning_time(monkeypatch):
    from secretops.plans import build_plan_artifact
    from secretops.recipes import load_recipe

    monkeypatch.setattr(secretops.plans, "utc_now", lambda: NOW)
    artifact = build_plan_artifact(
        load_recipe("examples/recipes/vault-secret-generate.yaml"),
        {"backend": "infisical", "domain": "https://eu.infisical.com",
         "project_id": "dummy-project", "path": "/"},
    )

    assert artifact.planned_at == "2026-11-14T12:00:00Z"
    assert artifact.approved_at is None


@pytest.mark.parametrize("value", [
    "2026-11-14 12:00:00", "2026-11-14T12:00:00+00:00", "2026-11-14T12:00:00.5Z",
    "2026-13-01T00:00:00Z", "",
])
def test_plan_times_must_have_the_fixed_form(broker, value):
    plan = approved(generate_plan, "bad-time", value)
    broker.write(plan)

    code, lines = broker.execute("bad-time")

    assert code == 1
    assert lines[0] == "FAIL plan file does not match the plan schema"
    assert broker.calls == []
