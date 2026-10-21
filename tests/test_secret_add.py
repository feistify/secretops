"""vault.secret.add: human secret entry through the broker endpoints.

Agents can plan, show, and preflight the action; only the human endpoints
approve it, and only together with the value. Values never appear in output,
plans, or audit.
"""

import json
import os
import subprocess
import sys

import pytest
from test_plans import TESTS_DIR, read_plan, write_plan

ADD_NAME = "SCALEWAY_API_KEY"
ADD_VALUE = "synthetic add value ü 42"
COMPETING_VALUE = "competing-writer-value"


def write_fake_infisical_for_add(tmp_path, *, existing=(), set_exit=0, store_other=False):
    """Stateful fake: `run` exposes stored names, `secrets set` stores (upsert)."""
    fake = tmp_path / "infisical"
    record_path = tmp_path / "add-record.json"
    fake.write_text(
        f"#!{sys.executable}\n"
        f"import sys; sys.path.insert(0, {TESTS_DIR!r})\n"
        "from fake_infisical import SetRejected, parse_set_args\n"
        "import json, os, subprocess\n"
        "from pathlib import Path\n"
        f"record_path = Path({str(record_path)!r})\n"
        "def load():\n"
        "    if record_path.exists():\n"
        "        return json.loads(record_path.read_text(encoding='utf-8'))\n"
        f"    return {{'stored': {{name: 'existing-value' for name in {list(existing)!r}}}, 'calls': []}}\n"
        "def save(record):\n"
        "    record_path.write_text(json.dumps(record), encoding='utf-8')\n"
        "args = sys.argv[1:]\n"
        "record = load()\n"
        "if 'run' in args:\n"
        "    record['calls'].append('run')\n"
        "    save(record)\n"
        "    command = args[args.index('--') + 1:]\n"
        "    child_env = dict(os.environ)\n"
        "    child_env.update(record['stored'])\n"
        "    raise SystemExit(subprocess.run(command, env=child_env).returncode)\n"
        "if 'secrets' in args and 'set' in args:\n"
        "    record['calls'].append('set')\n"
        "    record['set_args'] = args\n"
        "    try:\n"
        "        stored_now, _ = parse_set_args(args[args.index('set') + 1:])\n"
        "    except SetRejected:\n"
        "        save(record)\n"
        "        raise SystemExit(1)\n"
        f"    if {set_exit!r} == 0:\n"
        "        for name, value in stored_now.items():\n"
        f"            record['stored'][name] = {COMPETING_VALUE!r} if {store_other!r} else value\n"
        "    save(record)\n"
        f"    raise SystemExit({set_exit!r})\n"
        "raise SystemExit(99)\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    return record_path


def read_record(record_path):
    if not record_path.exists():
        return {"stored": {}, "calls": []}
    return json.loads(record_path.read_text(encoding="utf-8"))


@pytest.fixture
def broker(tmp_path):
    return Broker(tmp_path)


class Broker:
    """Runs secretops commands as the broker would, with fake providers."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.plan_dir = tmp_path / "plans"
        self.audit_path = tmp_path / "audit.jsonl"
        self.record_path = tmp_path / "add-record.json"
        self.extra_env = {}

    def fake_vault(self, **options):
        self.record_path = write_fake_infisical_for_add(self.tmp_path, **options)

    def env(self):
        env = os.environ.copy()
        env["SECRETOPS_PLAN_DIR"] = str(self.plan_dir)
        env["SECRETOPS_AUDIT_PATH"] = str(self.audit_path)
        fake = self.tmp_path / "infisical"
        if fake.exists():
            env["SECRETOPS_INFISICAL_BIN"] = str(fake)
        else:
            env["SECRETOPS_INFISICAL_BIN"] = "/nonexistent/infisical"
        env.update(self.extra_env)
        return env

    def run(self, *args, stdin=b""):
        result = subprocess.run(
            [sys.executable, "-m", "secretops.cli", *args],
            input=stdin,
            capture_output=True,
            env=self.env(),
            check=False,
        )
        return result

    def endpoint(self, *args, stdin=b""):
        result = self.run(*args, stdin=stdin)
        lines = result.stdout.decode("utf-8").splitlines()
        assert len(lines) == 1, result.stdout + result.stderr
        return result.returncode, json.loads(lines[0]), result

    def prepare(self, name=ADD_NAME, env="staging"):
        return self.endpoint("prepare-add", "--name", name, "--env", env)

    def submit(self, plan_id, digest, value=ADD_VALUE, **overrides):
        request = {"protocol": 1, "plan_id": plan_id, "digest": digest, "value": value}
        request.update(overrides)
        raw = (json.dumps(request) + "\n").encode("utf-8")
        return self.endpoint("submit-add", stdin=raw)

    def plan_path(self, plan_id):
        return self.plan_dir / f"{plan_id}.json"

    def audit_events(self):
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]

    def assert_no_value(self, *results, value=ADD_VALUE):
        for result in results:
            assert value.encode("utf-8") not in result.stdout
            assert value.encode("utf-8") not in result.stderr
            assert json.dumps(value).encode("utf-8") not in result.stdout
        for path in [self.audit_path, *self.plan_dir.glob("*.json")]:
            if path.exists():
                text = path.read_text(encoding="utf-8")
                assert value not in text
                assert json.dumps(value)[1:-1] not in text


def add_plan(plan_id="add-plan", status="planned", env="staging", name=ADD_NAME):
    return {
        "plan_id": plan_id,
        "action": "vault.secret.add",
        "secret_name": name,
        "env": env,
        "status": status,
        "secret_value_included": False,
        "create_mode": "create_only",
        "value_source": "human_prompt",
    }


# Direct entry: prepare, review, submit


def test_prepare_saves_metadata_only_plan_and_reviews_it(broker):
    broker.fake_vault()

    code, review, result = broker.prepare()

    assert code == 0, result.stderr
    assert review["outcome"] == "ready"
    assert review["atomic_create"] is False
    plan = review["plan"]
    assert plan["secret_name"] == ADD_NAME
    assert plan["env"] == "staging"
    assert plan["create_mode"] == "create_only"
    assert plan["value_source"] == "human_prompt"
    assert plan["vault_backend"] == "infisical"
    assert plan["vault_project_id"] == "dummy-project"
    saved = read_plan(broker.plan_path(plan["plan_id"]))
    assert saved["status"] == "planned"
    assert "approval_digest" not in saved
    assert set(saved) == {
        "plan_id", "action", "secret_name", "env", "status", "secret_value_included",
        "create_mode", "value_source", "contract_version", "vault_destination",
    }
    assert review["digest"].startswith("sha256-v1:")


def test_submit_creates_and_verifies_the_secret(broker):
    broker.fake_vault()
    _, review, prepared = broker.prepare()
    plan_id = review["plan"]["plan_id"]

    code, response, submitted = broker.submit(plan_id, review["digest"])

    assert code == 0, submitted.stderr
    assert response == {
        "protocol": 1, "outcome": "created", "message": "saved and verified", "plan_id": plan_id,
    }
    record = read_record(broker.record_path)
    assert record["stored"][ADD_NAME] == ADD_VALUE
    assert ADD_VALUE not in " ".join(record["set_args"])
    saved = read_plan(broker.plan_path(plan_id))
    assert saved["status"] == "executed"
    assert saved["approval_digest"] == review["digest"]
    broker.assert_no_value(prepared, submitted)


def test_audit_records_human_channel_and_outcomes_without_value(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    broker.submit(plan_id, review["digest"])

    events = [event for event in broker.audit_events() if event.get("plan_id") == plan_id]
    steps = [(event["step"], event["outcome"]) for event in events]
    assert steps == [
        ("prepare", "planned"),
        ("review", "ready"),
        ("approve", "approved"),
        ("submit", "created"),
    ]
    assert all(event["channel"] == "human" for event in events)
    assert all(event["secret_value_logged"] is False for event in events)
    for event in events:
        assert "value" not in event and "length" not in event and "hash" not in event
    digest = review["digest"]
    by_step = {event["step"]: event for event in events}
    assert by_step["review"]["reviewed_digest"] == digest
    assert by_step["approve"]["reviewed_digest"] == digest
    assert by_step["approve"]["approval_digest"] == digest
    assert by_step["submit"]["reviewed_digest"] == digest
    assert "reviewed_digest" not in by_step["prepare"]


def test_refused_submission_audits_the_digest_the_human_reviewed(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    path = broker.plan_path(plan_id)
    plan = read_plan(path)
    plan["env"] = "dev"
    path.write_text(json.dumps(plan), encoding="utf-8")

    broker.submit(plan_id, review["digest"])

    [event] = [e for e in broker.audit_events() if e.get("step") == "submit"]
    assert event["outcome"] == "refused"
    assert event["reviewed_digest"] == review["digest"]
    assert "approval_digest" not in event


def test_unicode_and_meaningful_spaces_round_trip_unchanged(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    value = "  lead and trail  é中\U0001f511 "

    code, _, _ = broker.submit(review["plan"]["plan_id"], review["digest"], value=value)

    assert code == 0
    assert read_record(broker.record_path)["stored"][ADD_NAME] == value


# Early refusals happen before any value is requested


def test_production_is_denied_without_plan_or_provider_call(broker):
    broker.fake_vault()

    code, review, _ = broker.prepare(env="production")

    assert code == 1
    assert review["outcome"] == "refused"
    assert "production execution denied by policy" in review["message"]
    assert not list(broker.plan_dir.glob("*.json"))
    assert read_record(broker.record_path)["calls"] == []


def test_existing_secret_is_reported_at_review(broker):
    broker.fake_vault(existing=[ADD_NAME])

    code, review, _ = broker.prepare()

    assert code == 1
    assert review["outcome"] == "exists"
    assert review["digest"] is None
    saved = read_plan(broker.plan_path(review["plan"]["plan_id"]))
    assert saved["status"] == "planned"


def test_provider_failure_at_review_is_reported_before_value(broker):
    # No fake CLI: the configured executable does not exist.
    code, review, _ = broker.prepare()

    assert code == 1
    assert review["outcome"] == "provider_error"
    assert review["digest"] is None


@pytest.mark.parametrize("name,env", [("bad-name", "staging"), (ADD_NAME, "stag ing")])
def test_invalid_metadata_is_refused(broker, name, env):
    broker.fake_vault()

    code, review, _ = broker.prepare(name=name, env=env)

    assert code == 1
    assert review["outcome"] == "refused"
    assert not list(broker.plan_dir.glob("*.json"))


# Agent-prepared plans


def write_add_recipe(tmp_path, **fields):
    recipe = tmp_path / "add.yaml"
    content = {"action": "vault.secret.add", "secret_name": ADD_NAME, "env": "staging"}
    content.update(fields)
    recipe.write_text("".join(f"{key}: {value}\n" for key, value in content.items()), encoding="utf-8")
    return recipe


def test_agent_prepared_plan_is_completed_by_review_and_submit(broker):
    broker.fake_vault()
    recipe = write_add_recipe(broker.tmp_path)

    planned = broker.run("plan", "--file", str(recipe), "--save")
    assert planned.returncode == 0, planned.stderr
    plan_id = next(
        line.split(": ", 1)[1] for line in planned.stdout.decode().splitlines() if "plan_id:" in line
    )
    assert read_record(broker.record_path)["calls"] == []

    code, review, _ = broker.endpoint("review-add", "--id", plan_id)
    assert code == 0 and review["outcome"] == "ready"
    code, response, _ = broker.submit(plan_id, review["digest"])

    assert response["outcome"] == "created"
    assert read_plan(broker.plan_path(plan_id))["status"] == "executed"


def test_recipe_rejects_other_value_sources(broker):
    recipe = write_add_recipe(broker.tmp_path, value_source="environment")

    result = broker.run("plan", "--file", str(recipe), "--save")

    assert result.returncode != 0
    assert not list(broker.plan_dir.glob("*.json"))


def test_show_and_preflight_describe_the_plan_for_agents(broker):
    broker.fake_vault()
    write_plan(broker.plan_dir, add_plan())

    shown = broker.run("show-plan", "--id", "add-plan")
    preflight = broker.run("preflight-plan", "--id", "add-plan")

    assert "value_source: human_prompt" in shown.stdout.decode()
    assert "create_mode: create_only" in shown.stdout.decode()
    assert preflight.returncode == 0
    assert "secretops add --plan add-plan" in preflight.stdout.decode()
    assert read_plan(broker.plan_path("add-plan"))["status"] == "planned"


def test_execute_plan_refuses_without_changing_the_plan(broker):
    broker.fake_vault()
    write_plan(broker.plan_dir, add_plan())

    result = broker.run("execute-plan", "--id", "add-plan")

    assert result.returncode == 1
    assert "requires human completion" in result.stdout.decode()
    assert "secretops add --plan add-plan" in result.stdout.decode()
    assert read_plan(broker.plan_path("add-plan"))["status"] == "planned"
    assert read_record(broker.record_path)["calls"] == []


def test_approve_plan_points_to_secretops_add(broker):
    write_plan(broker.plan_dir, add_plan())

    result = broker.run("approve-plan", "--id", "add-plan")

    assert result.returncode == 1
    assert "use: secretops add --plan add-plan" in result.stdout.decode()
    assert read_plan(broker.plan_path("add-plan"))["status"] == "planned"


def test_rejected_plan_cannot_be_submitted(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]

    rejected = broker.run("reject-plan", "--id", plan_id)
    _, response, _ = broker.submit(plan_id, review["digest"])

    assert rejected.returncode == 0
    assert response["outcome"] == "refused"
    assert "status is rejected" in response["message"]
    assert read_plan(broker.plan_path(plan_id))["status"] == "rejected"
    assert "set" not in read_record(broker.record_path)["calls"]


def test_reject_waits_for_no_one_while_a_submission_holds_the_lock(broker):
    write_plan(broker.plan_dir, add_plan())
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time; from secretops.plans import plan_lock\n"
                "with plan_lock('add-plan'):\n"
                "    print('locked', flush=True); time.sleep(30)"
            ),
        ],
        env=broker.env(), stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        result = broker.run("reject-plan", "--id", "add-plan")
        _, response, _ = broker.submit("add-plan", "sha256-v1:" + "0" * 64)
    finally:
        holder.kill()
        holder.wait()

    assert result.returncode == 1
    assert "in use" in result.stdout.decode()
    assert response["outcome"] == "refused" and "in use" in response["message"]
    assert read_plan(broker.plan_path("add-plan"))["status"] == "planned"


# Final checks at submission


def test_changed_plan_after_review_is_refused_before_provider_calls(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    path = broker.plan_path(plan_id)
    plan = read_plan(path)
    plan["secret_name"] = "OTHER_NAME"
    path.write_text(json.dumps(plan), encoding="utf-8")
    calls_before = list(read_record(broker.record_path)["calls"])

    _, response, _ = broker.submit(plan_id, review["digest"])

    assert response["outcome"] == "refused"
    assert "changed after review" in response["message"]
    assert read_record(broker.record_path)["calls"] == calls_before
    assert read_plan(path)["status"] == "planned"


def test_changed_destination_after_review_is_refused_before_provider_calls(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    calls_before = list(read_record(broker.record_path)["calls"])
    broker.extra_env["SECRETOPS_INFISICAL_PROJECT_ID"] = "other-project"

    _, response, _ = broker.submit(plan_id, review["digest"])

    assert response["outcome"] == "refused"
    assert "vault destination differs" in response["message"]
    assert read_record(broker.record_path)["calls"] == calls_before
    assert read_plan(broker.plan_path(plan_id))["status"] == "planned"


def test_policy_change_after_review_is_refused(broker, tmp_path):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    policy = tmp_path / "policy.yaml"
    policy.write_text("allowed_actions: []\ndenied_actions: []\n", encoding="utf-8")
    broker.extra_env["SECRETOPS_POLICY_PATH"] = str(policy)

    _, response, _ = broker.submit(review["plan"]["plan_id"], review["digest"])

    assert response["outcome"] == "refused"
    assert "set" not in read_record(broker.record_path)["calls"]


def test_secret_created_after_review_is_not_written(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    record = read_record(broker.record_path)
    record["stored"][ADD_NAME] = COMPETING_VALUE
    broker.record_path.write_text(json.dumps(record), encoding="utf-8")

    _, response, _ = broker.submit(plan_id, review["digest"])

    assert response["outcome"] == "exists"
    assert "nothing was written" in response["message"]
    assert read_record(broker.record_path)["stored"][ADD_NAME] == COMPETING_VALUE
    assert read_plan(broker.plan_path(plan_id))["status"] == "failed"


def test_executed_plan_cannot_be_replayed(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    broker.submit(plan_id, review["digest"])

    _, response, _ = broker.submit(plan_id, review["digest"], value="second-value")

    assert response["outcome"] == "refused"
    assert "status is executed" in response["message"]
    assert read_record(broker.record_path)["stored"][ADD_NAME] == ADD_VALUE


@pytest.mark.parametrize(
    "options",
    [{"set_exit": 1}, {"store_other": True}],
    ids=["cli-failure-after-launch", "read-back-mismatch"],
)
def test_unconfirmed_write_is_unknown_and_not_retried(broker, options):
    broker.fake_vault(**options)
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]

    code, response, submitted = broker.submit(plan_id, review["digest"])

    assert code == 1
    assert response["outcome"] == "unknown"
    assert "before any new attempt" in response["message"]
    assert read_record(broker.record_path)["calls"].count("set") == 1
    assert read_plan(broker.plan_path(plan_id))["status"] == "failed"
    events = broker.audit_events()
    assert events[-1]["status"] == "outcome_unknown" and events[-1]["outcome"] == "unknown"
    broker.assert_no_value(submitted)


# Input validation and request framing: nothing is written


@pytest.mark.parametrize(
    "value",
    ["", "   ", "line one\nline two", "line\r", "nul\x00byte", "tab\tvalue",
     "sep arator", "x" * (16 * 1024 + 1)],
    ids=["empty", "whitespace", "lf", "cr", "nul", "tab", "line-separator", "too-long"],
)
def test_invalid_values_are_refused_and_plan_stays_planned(broker, value):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]

    code, response, submitted = broker.submit(plan_id, review["digest"], value=value)

    assert code == 1
    assert response["outcome"] == "refused"
    assert response["message"].startswith("invalid value:")
    assert "set" not in read_record(broker.record_path)["calls"]
    assert read_plan(broker.plan_path(plan_id))["status"] == "planned"
    if value.strip():
        broker.assert_no_value(submitted, value=value)


def test_largest_allowed_value_is_accepted(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()

    _, response, _ = broker.submit(review["plan"]["plan_id"], review["digest"], value="v" * 16 * 1024)

    assert response["outcome"] == "created"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json\n",
        b'{"protocol": 1}\n',
        b'{"protocol": 2, "plan_id": "p", "digest": "sha256-v1:x", "value": "v"}\n',
        b'{"protocol": 1, "plan_id": "p", "digest": "sha256-v1:x", "value": "v", "extra": 1}\n',
        b'{"protocol": 1, "plan_id": "p", "plan_id": "p", "digest": "sha256-v1:x", "value": "v"}\n',
        b'{"protocol": true, "plan_id": "p", "digest": "sha256-v1:x", "value": "v"}\n',
        b'{"protocol": 1, "plan_id": "p", "digest": "sha256-v1:x", "value": "v"}',
        b'{"protocol": 1, "plan_id": "p", "digest": "sha256-v1:x", "value": "v"}\n{}\n',
        b'{"protocol": 1, "plan_id": "../x", "digest": "sha256-v1:x", "value": "v"}\n',
        b"\xff\xfe\n",
        b"{" + b" " * (64 * 1024) + b"}\n",
    ],
    ids=["empty", "not-json", "missing-fields", "version", "unknown-field", "duplicate",
         "bool-version", "no-newline", "trailing-data", "path-plan-id", "invalid-utf8",
         "oversize"],
)
def test_malformed_requests_are_refused_without_provider_calls(broker, raw):
    broker.fake_vault()

    code, response, _ = broker.endpoint("submit-add", stdin=raw)

    assert code == 1
    assert response["outcome"] == "refused"
    assert read_record(broker.record_path)["calls"] == []


def test_incomplete_request_times_out(broker):
    from secretops.human_entry import RequestError, read_request

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"protocol": 1')
        with pytest.raises(RequestError, match="did not complete in time"):
            read_request(read_fd, deadline_seconds=0.2)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_concurrent_submissions_cannot_both_write(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    request = (json.dumps({
        "protocol": 1, "plan_id": plan_id, "digest": review["digest"], "value": ADD_VALUE,
    }) + "\n").encode()
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "secretops.cli", "submit-add"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=broker.env(),
        )
        for _ in range(4)
    ]
    outcomes = []
    for process in processes:
        stdout, _ = process.communicate(request)
        outcomes.append(json.loads(stdout)["outcome"])

    assert outcomes.count("created") == 1
    assert read_record(broker.record_path)["calls"].count("set") == 1
