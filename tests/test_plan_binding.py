"""Plan destination binding: contract version, vault and GitHub destinations,
approval digest, strict plan schema, and single-load approval and execution
(docs/design/plan-destination-binding.md)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from secretops import plan_lifecycle, plans
from secretops.plans import parse_plan_json, plan_digest, validate_plan_content
from secretops.vaults import VaultConfigurationError
from secretops.vaults.infisical_cli import InfisicalCliVault, InfisicalConnection, canonical_domain
from test_plans import TESTS_DIR, generate_plan, read_plan, run_secretops, write_plan

FIXTURES = Path(TESTS_DIR) / "fixtures"
FIXTURE_DIGEST = "sha256-v1:4539f0de84a45e2d2fa9d23dfe0ff0fa3368bf3ac32ea18853eef380a2e7737e"
GENERATE_RECIPE = (
    "action: vault.secret.generate\n"
    "env: dev\n"
    "secret_name: GENERATED_DUMMY_PASSWORD\n"
    "create_mode: create_only\n"
    "generator:\n"
    "  type: password\n"
    "  length: 32\n"
)
VERIFY_MANY_RECIPE = (
    "action: github.env_secret.verify_many\n"
    "github_owner: example-owner\n"
    "github_repo: secretops\n"
    "github_environment: staging\n"
    "secrets:\n"
    "  - name: BACKUP_S3_BUCKET\n"
)


def write_stateful_infisical(tmp_path):
    """Fake CLI with a vault keyed by (domain, project, path, env, name).

    Every call is appended to calls.jsonl, so tests can count provider calls
    and check the destination of each one.
    """
    fake = tmp_path / "bin" / "infisical"
    fake.parent.mkdir(exist_ok=True)
    state = tmp_path / "vault-state.json"
    calls = tmp_path / "calls.jsonl"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        f"sys.path.insert(0, {TESTS_DIR!r})\n"
        "from fake_infisical import SetRejected, parse_set_args\n"
        f"state_path, calls_path = {str(state)!r}, {str(calls)!r}\n"
        "argv = sys.argv[1:]\n"
        "with open(calls_path, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(argv) + '\\n')\n"
        "domain = argv[argv.index('--domain') + 1]\n"
        "def option(prefix):\n"
        "    return next(arg.split('=', 1)[1] for arg in argv if arg.startswith(prefix + '='))\n"
        "state = json.load(open(state_path)) if os.path.exists(state_path) else {}\n"
        "if 'run' in argv[:4]:\n"
        "    env = option('--env')\n"
        "    key = '|'.join([domain, option('--projectId'), option('--path'), env])\n"
        "    command = argv[argv.index('--') + 1:]\n"
        "    child_env = dict(os.environ)\n"
        "    child_env.update(state.get(key, {}))\n"
        "    raise SystemExit(subprocess.run(command, env=child_env).returncode)\n"
        "rest = argv[argv.index('set') + 1:]\n"
        "env = rest[rest.index('--env') + 1]\n"
        "key = '|'.join([domain, option('--projectId'), option('--path'), env])\n"
        "try:\n"
        "    stored, _ = parse_set_args([arg for arg in rest if not arg.startswith('--projectId') and not arg.startswith('--path') and not arg.startswith('--type')])\n"
        "except SetRejected:\n"
        "    raise SystemExit(1)\n"
        "state.setdefault(key, {}).update(stored)\n"
        "json.dump(state, open(state_path, 'w'))\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    return fake.parent, calls, state


def provider_calls(calls_path):
    if not calls_path.exists():
        return []
    return [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]


def write_recording_gh(tmp_path, names=("BACKUP_S3_BUCKET",)):
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir(exist_ok=True)
    record = tmp_path / "gh-calls.jsonl"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(record)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'args': sys.argv[1:], 'gh_vars': sorted(k for k in os.environ if k in ('GH_HOST', 'GH_REPO'))}) + '\\n')\n"
        f"for name in {list(names)!r}:\n"
        "    print(name + '\\t2026-01-01T00:00:00Z')\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    return fake.parent, record


def save_plan_from_recipe(tmp_path, plan_dir, recipe_text, **kwargs):
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(recipe_text, encoding="utf-8")
    result = run_secretops("plan", "--file", str(recipe), "--save", plan_dir=plan_dir, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    plan_id = result.stdout.strip().splitlines()[-1].split("plan_id: ")[1]
    return plan_id, plan_dir / f"{plan_id}.json"


@pytest.fixture
def vault_cli(tmp_path, monkeypatch):
    bin_dir, calls, state = write_stateful_infisical(tmp_path)
    monkeypatch.setenv("SECRETOPS_INFISICAL_BIN", str(bin_dir / "infisical"))
    return calls, state


def run(*args, tmp_path, plan_dir, **kwargs):
    return run_secretops(*args, plan_dir=plan_dir, audit_path=tmp_path / "audit.jsonl", **kwargs)


# 1, 2: planning records the destination, without a token or provider call


def test_saved_vault_plan_records_canonical_destination_without_credentials(tmp_path, monkeypatch, vault_cli):
    calls, _ = vault_cli
    monkeypatch.setenv("INFISICAL_DOMAIN", "HTTPS://Vault.Example.COM:443/infisical/")
    monkeypatch.setenv("SECRETOPS_PROVIDER_TIMEOUT_SECONDS", "17")
    monkeypatch.delenv("INFISICAL_TOKEN")
    plan_dir = tmp_path / "plans"

    _, plan_path = save_plan_from_recipe(tmp_path, plan_dir, GENERATE_RECIPE)

    plan = read_plan(plan_path)
    assert plan["contract_version"] == 1
    assert plan["vault_destination"] == {
        "backend": "infisical",
        "domain": "https://vault.example.com/infisical",
        "project_id": "dummy-project",
        "path": "/",
    }
    text = plan_path.read_text(encoding="utf-8")
    assert "infisical-bin" not in text and str(tmp_path / "bin") not in text
    assert "17" not in json.dumps(plan["vault_destination"])
    assert "approval_digest" not in plan
    assert provider_calls(calls) == []


@pytest.mark.parametrize("missing", ["SECRETOPS_INFISICAL_PROJECT_ID", "SECRETOPS_INFISICAL_PATH"])
def test_vault_plan_with_missing_destination_is_not_saved(tmp_path, monkeypatch, vault_cli, missing):
    calls, _ = vault_cli
    monkeypatch.delenv(missing)
    plan_dir = tmp_path / "plans"
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(GENERATE_RECIPE, encoding="utf-8")

    result = run_secretops("plan", "--file", str(recipe), "--save", plan_dir=plan_dir)

    assert result.returncode != 0
    assert "no plan was saved" in result.stdout
    assert not plan_dir.exists() or list(plan_dir.iterdir()) == []
    assert provider_calls(calls) == []


@pytest.mark.parametrize("domain", ["https://user:pass@vault.example.com", "https://vault.example.com/?x=1", "https://vault.example.com#frag"])
def test_domain_with_credentials_query_or_fragment_is_rejected_at_planning(tmp_path, monkeypatch, vault_cli, domain):
    monkeypatch.setenv("INFISICAL_DOMAIN", domain)
    plan_dir = tmp_path / "plans"
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(GENERATE_RECIPE, encoding="utf-8")

    result = run_secretops("plan", "--file", str(recipe), "--save", plan_dir=plan_dir)

    assert result.returncode != 0
    assert "pass" not in result.stdout + result.stderr
    assert not plan_dir.exists() or list(plan_dir.iterdir()) == []


# 3, 4, 5: configuration changes after planning


@pytest.mark.parametrize(
    "name,value",
    [
        ("INFISICAL_DOMAIN", "https://other.example.com"),
        ("SECRETOPS_INFISICAL_PROJECT_ID", "other-project"),
        ("SECRETOPS_INFISICAL_PATH", "/other"),
    ],
)
def test_changed_destination_blocks_preflight_and_execute_without_provider_calls(
    tmp_path, monkeypatch, vault_cli, name, value
):
    calls, _ = vault_cli
    plan_dir = tmp_path / "plans"
    plan_id, plan_path = save_plan_from_recipe(tmp_path, plan_dir, GENERATE_RECIPE)
    assert run("approve-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir).returncode == 0
    monkeypatch.setenv(name, value)

    preflight = run("preflight-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    assert preflight.returncode != 0
    assert "vault destination differs from configuration" in preflight.stdout
    assert read_plan(plan_path)["status"] == "approved"

    execute = run("execute-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    assert execute.returncode != 0
    assert "vault destination differs from configuration" in execute.stdout
    assert read_plan(plan_path)["status"] == "failed"
    assert provider_calls(calls) == []


def test_configuration_change_after_passing_preflight_blocks_execute(tmp_path, monkeypatch, vault_cli):
    calls, _ = vault_cli
    plan_dir = tmp_path / "plans"
    plan_id, plan_path = save_plan_from_recipe(tmp_path, plan_dir, GENERATE_RECIPE)
    run("approve-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    preflight = run("preflight-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    assert preflight.returncode == 0, preflight.stdout
    calls_after_preflight = len(provider_calls(calls))
    assert calls_after_preflight > 0

    monkeypatch.setenv("SECRETOPS_INFISICAL_PROJECT_ID", "other-project")
    execute = run("execute-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)

    assert execute.returncode != 0
    assert "project_id" in execute.stdout
    assert len(provider_calls(calls)) == calls_after_preflight
    assert read_plan(plan_path)["status"] == "failed"


def test_replaced_token_for_same_destination_is_allowed(tmp_path, monkeypatch, vault_cli):
    calls, state = vault_cli
    plan_dir = tmp_path / "plans"
    audit = tmp_path / "audit.jsonl"
    plan_id, plan_path = save_plan_from_recipe(tmp_path, plan_dir, GENERATE_RECIPE)
    run("approve-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    monkeypatch.setenv("INFISICAL_TOKEN", "renewed-dummy-token")

    preflight = run("preflight-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    execute = run("execute-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)

    assert preflight.returncode == 0, preflight.stdout
    assert execute.returncode == 0, execute.stdout
    assert read_plan(plan_path)["status"] == "executed"
    stored = json.loads(state.read_text(encoding="utf-8"))
    value = stored["https://eu.infisical.com|dummy-project|/|dev"]["GENERATED_DUMMY_PASSWORD"]
    for text in (preflight.stdout, execute.stdout, plan_path.read_text(), audit.read_text()):
        assert value not in text
    assert not any(value in arg for call in provider_calls(calls) for arg in call)


# 6: canonical domain


@pytest.mark.parametrize(
    "url,canonical",
    [
        ("https://eu.infisical.com", "https://eu.infisical.com"),
        ("HTTPS://EU.Infisical.COM:443/", "https://eu.infisical.com"),
        ("http://192.0.2.94:80", "http://192.0.2.94"),
        ("http://192.0.2.94:8086/", "http://192.0.2.94:8086"),
        ("https://vault.example.com/base/", "https://vault.example.com/base"),
        ("https://[::1]:8443", "https://[::1]:8443"),
    ],
)
def test_canonical_domain_normalises_case_default_port_and_trailing_slash(url, canonical):
    assert canonical_domain(url) == canonical


@pytest.mark.parametrize(
    "first,second",
    [
        ("https://vault.example.com", "https://vault2.example.com"),
        ("https://vault.example.com", "http://vault.example.com"),
        ("https://vault.example.com", "https://vault.example.com:8443"),
        ("https://vault.example.com/a", "https://vault.example.com/b"),
        ("https://vault.example.com", "https://203.0.113.10"),
    ],
)
def test_canonical_domain_keeps_different_destinations_different(first, second):
    assert canonical_domain(first) != canonical_domain(second)


@pytest.mark.parametrize(
    "url",
    [
        "https://user@vault.example.com",
        "https://user:pass@vault.example.com",
        "https://vault.example.com/?",
        "https://vault.example.com/?a=b",
        "https://vault.example.com/#x",
        "ftp://vault.example.com",
        "https://",
        "vault.example.com",
        " https://vault.example.com",
        "https://vault.example.com:99999",
    ],
)
def test_canonical_domain_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(VaultConfigurationError):
        canonical_domain(url)


# 7: one destination per operation


def test_bound_adapter_ignores_environment_changes_during_an_operation():
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        nonce = command[command.index("-c") + 2]
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"nonce": nonce, "present": False}) + "\n", stderr=""
        )

    environ = {
        "INFISICAL_TOKEN": "dummy",
        "SECRETOPS_INFISICAL_PROJECT_ID": "bound-project",
        "SECRETOPS_INFISICAL_PATH": "/bound",
    }
    connection = InfisicalConnection(domain="https://bound.example.com", project_id="bound-project", path="/bound")
    vault = InfisicalCliVault(runner=runner, binary="infisical", environ=environ, connection=connection)

    vault.secret_exists("FIRST_SECRET", "dev")
    environ.update(
        {
            "INFISICAL_DOMAIN": "https://other.example.com",
            "SECRETOPS_INFISICAL_PROJECT_ID": "other-project",
            "SECRETOPS_INFISICAL_PATH": "/other",
        }
    )
    vault.secret_exists("SECOND_SECRET", "dev")

    for command in commands:
        assert command[command.index("--domain") + 1] == "https://bound.example.com"
        assert "--projectId=bound-project" in command
        assert "--path=/bound" in command


@pytest.mark.parametrize(
    "destination",
    [
        {"backend": "openbao", "domain": "https://x.example.com", "project_id": "p", "path": "/"},
        {"backend": "infisical", "domain": "HTTPS://X.example.com", "project_id": "p", "path": "/"},
        {"backend": "infisical", "domain": "https://x.example.com", "project_id": "", "path": "/"},
        {"backend": "infisical", "domain": "https://x.example.com", "project_id": "p", "path": "rel"},
        {"backend": "infisical", "domain": "https://x.example.com", "project_id": "p"},
    ],
)
def test_recorded_destination_must_be_valid_and_canonical(destination):
    with pytest.raises(VaultConfigurationError):
        InfisicalConnection.from_destination(destination)


# 8, 9: approval digest


def test_approval_stores_digest_of_the_operation_content(tmp_path, vault_cli):
    plan_dir = tmp_path / "plans"
    plan_id, plan_path = save_plan_from_recipe(tmp_path, plan_dir, GENERATE_RECIPE)
    before = read_plan(plan_path)

    result = run("approve-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    show = run("show-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)

    after = read_plan(plan_path)
    assert result.returncode == 0
    assert after["approval_digest"] == plan_digest(before)
    assert f"approval_digest: {after['approval_digest']}" in result.stdout
    assert f"plan_digest: {after['approval_digest']}" in show.stdout
    assert "vault_project_id: dummy-project" in show.stdout
    assert {key: value for key, value in after.items() if key not in ("status", "approval_digest")} == {
        key: value for key, value in before.items() if key != "status"
    }


def test_status_is_not_part_of_the_digest():
    plan = json.loads((FIXTURES / "plan_v1_copy_many.json").read_text(encoding="utf-8"))
    changed = dict(plan, status="failed", approval_digest=FIXTURE_DIGEST)
    assert plan_digest(changed) == plan_digest(plan)


@pytest.mark.parametrize(
    "change",
    [
        lambda plan: plan.update(secret_name="OTHER_SECRET"),
        lambda plan: plan.update(env="staging"),
        lambda plan: plan.update(create_mode="overwrite"),
        lambda plan: plan["generator"].update(length=8),
        lambda plan: plan["vault_destination"].update(project_id="other-project"),
        lambda plan: plan["vault_destination"].update(path="/other"),
    ],
)
def test_content_change_after_approval_blocks_preflight_and_execute(tmp_path, vault_cli, change):
    calls, _ = vault_cli
    plan_dir = tmp_path / "plans"
    plan_id, plan_path = save_plan_from_recipe(tmp_path, plan_dir, GENERATE_RECIPE)
    run("approve-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    plan = read_plan(plan_path)
    change(plan)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    for command in ("preflight-plan", "execute-plan"):
        result = run(command, "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
        assert result.returncode != 0
        assert "plan content changed after approval" in result.stdout
    assert provider_calls(calls) == []
    assert read_plan(plan_path)["status"] == "failed"


def test_approved_plan_without_digest_is_refused(tmp_path, vault_cli):
    calls, _ = vault_cli
    plan_dir = tmp_path / "plans"
    plan = generate_plan("no-digest", status="approved")
    plan_path = write_plan(plan_dir, plan)
    data = read_plan(plan_path)
    del data["approval_digest"]
    plan_path.write_text(json.dumps(data), encoding="utf-8")

    for command in ("preflight-plan", "execute-plan"):
        result = run(command, "--id", "no-digest", tmp_path=tmp_path, plan_dir=plan_dir)
        assert result.returncode != 0
        assert "approved plan has no approval digest" in result.stdout
    assert provider_calls(calls) == []


def test_unknown_digest_prefix_is_refused(tmp_path, vault_cli):
    calls, _ = vault_cli
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, generate_plan("other-prefix", status="approved"))
    data = read_plan(plan_path)
    data["approval_digest"] = "sha256-v2:" + data["approval_digest"].split(":", 1)[1]
    plan_path.write_text(json.dumps(data), encoding="utf-8")

    for command in ("preflight-plan", "execute-plan"):
        result = run(command, "--id", "other-prefix", tmp_path=tmp_path, plan_dir=plan_dir)
        assert result.returncode != 0
    assert provider_calls(calls) == []


# 10: plans from before destination binding, and other contract versions


def test_legacy_plan_is_shown_but_never_approved_or_run(tmp_path, vault_cli):
    calls, _ = vault_cli
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, generate_plan("legacy-plan"), legacy=True)
    original = plan_path.read_bytes()

    show = run("show-plan", "--id", "legacy-plan", tmp_path=tmp_path, plan_dir=plan_dir)
    assert show.returncode == 0
    assert "action: vault.secret.generate" in show.stdout
    assert "replan required" in show.stdout

    for command in ("approve-plan", "preflight-plan", "execute-plan"):
        result = run(command, "--id", "legacy-plan", tmp_path=tmp_path, plan_dir=plan_dir)
        assert result.returncode != 0
        assert "replan required" in result.stdout
    assert plan_path.read_bytes() == original
    assert provider_calls(calls) == []


def test_other_contract_version_is_refused(tmp_path, vault_cli):
    calls, _ = vault_cli
    plan_dir = tmp_path / "plans"
    plan = generate_plan("future-plan")
    plan["contract_version"] = 2
    plan_path = write_plan(plan_dir, plan)
    original = plan_path.read_bytes()

    for command in ("show-plan", "approve-plan", "preflight-plan", "execute-plan"):
        result = run(command, "--id", "future-plan", tmp_path=tmp_path, plan_dir=plan_dir)
        assert result.returncode != 0
        assert "contract version is not supported" in result.stdout
    assert plan_path.read_bytes() == original
    assert provider_calls(calls) == []


# 11: strict schema


def _approved_generate_text():
    return json.dumps(
        {
            "plan_id": "strict-plan",
            "action": "vault.secret.generate",
            "secret_name": "GENERATED_DUMMY_PASSWORD",
            "env": "dev",
            "status": "planned",
            "secret_value_included": False,
            "create_mode": "create_only",
            "generator": {"type": "password", "length": 32},
            "contract_version": 1,
            "vault_destination": {
                "backend": "infisical",
                "domain": "https://eu.infisical.com",
                "project_id": "dummy-project",
                "path": "/",
            },
        }
    )


STRICT_SCHEMA_CASES = {
    "duplicate_key": lambda text: text.replace('"env": "dev"', '"env": "dev", "env": "prod"'),
    "nested_duplicate_key": lambda text: text.replace('"length": 32', '"length": 32, "length": 8'),
    "unknown_field": lambda text: text.replace('"env": "dev"', '"env": "dev", "note": "SENSITIVE_MARKER"'),
    "foreign_field": lambda text: text.replace('"env": "dev"', '"env": "dev", "source_env": "dev"'),
    "unknown_generator_field": lambda text: text.replace('"length": 32', '"length": 32, "charset": "x"'),
    "bool_for_int": lambda text: text.replace('"length": 32', '"length": true'),
    "bool_for_version": lambda text: text.replace('"contract_version": 1', '"contract_version": true'),
    "float": lambda text: text.replace('"length": 32', '"length": 32.0'),
    "null": lambda text: text.replace('"create_mode": "create_only"', '"create_mode": null'),
    "nan": lambda text: text.replace('"length": 32', '"length": NaN'),
    "infinity": lambda text: text.replace('"length": 32', '"length": Infinity'),
    "string_for_bool": lambda text: text.replace('"secret_value_included": false', '"secret_value_included": "false"'),
    "value_included": lambda text: text.replace('"secret_value_included": false', '"secret_value_included": true'),
    "padded_name": lambda text: text.replace('"GENERATED_DUMMY_PASSWORD"', '" GENERATED_DUMMY_PASSWORD"'),
    "missing_destination_field": lambda text: text.replace('"path": "/"', '"other": "/"'),
    "wrong_plan_id": lambda text: text.replace('"strict-plan"', '"another-plan"'),
    "not_an_object": lambda text: "[" + text + "]",
    "invalid_json": lambda text: text[:-1],
    "invalid_utf8": lambda text: text.replace('"dev"', '"d\udcffv"', 1),
}


@pytest.mark.parametrize("case", sorted(STRICT_SCHEMA_CASES))
def test_strict_schema_violations_are_refused_and_file_is_untouched(tmp_path, vault_cli, case):
    calls, _ = vault_cli
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_path = plan_dir / "strict-plan.json"
    content = STRICT_SCHEMA_CASES[case](_approved_generate_text())
    plan_path.write_bytes(content.encode("utf-8", errors="surrogateescape"))
    original = plan_path.read_bytes()
    audit = tmp_path / "audit.jsonl"

    for command in ("approve-plan", "preflight-plan", "execute-plan"):
        result = run(command, "--id", "strict-plan", tmp_path=tmp_path, plan_dir=plan_dir)
        output = result.stdout + result.stderr
        assert result.returncode != 0, (command, output)
        assert "FAIL plan" in output
        assert "SENSITIVE_MARKER" not in output
        assert "GENERATED_DUMMY_PASSWORD" not in output

    assert plan_path.read_bytes() == original
    assert provider_calls(calls) == []
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 3
    for event in events:
        assert event["secret_name"] == "unknown"
        assert event["env"] == "unknown"
        assert event["plan_id"] == "strict-plan"
    assert "SENSITIVE_MARKER" not in audit.read_text(encoding="utf-8")
    assert "GENERATED_DUMMY_PASSWORD" not in audit.read_text(encoding="utf-8")


def test_strict_schema_accepts_the_unmodified_plan(tmp_path):
    validate_plan_content(parse_plan_json(_approved_generate_text()), "strict-plan")


def test_new_plans_with_invalid_generator_are_not_saved(tmp_path, vault_cli):
    plan_dir = tmp_path / "plans"
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(GENERATE_RECIPE.replace("length: 32", "length: '32'"), encoding="utf-8")

    result = run_secretops("plan", "--file", str(recipe), "--save", plan_dir=plan_dir)

    assert result.returncode != 0
    assert "no plan was saved" in result.stdout
    assert not plan_dir.exists() or list(plan_dir.iterdir()) == []


# 12: fixed example plan


def test_fixed_example_plan_produces_recorded_digest():
    data = parse_plan_json((FIXTURES / "plan_v1_copy_many.json").read_text(encoding="utf-8"))
    validate_plan_content(data, "fixture-copy-many")
    assert plan_digest(data) == FIXTURE_DIGEST


# 13: one parsed plan per command


def test_approval_writes_status_and_digest_from_the_checked_content(tmp_path, monkeypatch, vault_cli):
    plan_dir = tmp_path / "plans"
    plan_id, plan_path = save_plan_from_recipe(tmp_path, plan_dir, GENERATE_RECIPE)
    monkeypatch.setenv("SECRETOPS_PLAN_DIR", str(plan_dir))
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    real_digest = plan_lifecycle.plan_digest

    def digest_then_tamper(content):
        digest = real_digest(content)
        tampered = read_plan(plan_path)
        tampered["secret_name"] = "TAMPERED_SECRET"
        plan_path.write_text(json.dumps(tampered), encoding="utf-8")
        return digest

    monkeypatch.setattr(plan_lifecycle, "plan_digest", digest_then_tamper)
    plan_lifecycle.transition_planned_plan(plan_id, "approved", "approved", Console())

    approved = read_plan(plan_path)
    assert approved["status"] == "approved"
    assert approved["secret_name"] == "GENERATED_DUMMY_PASSWORD"
    assert plan_digest(approved) == approved["approval_digest"]


def test_execution_uses_the_checked_plan_even_if_the_file_changes(tmp_path, monkeypatch, vault_cli):
    calls, state = vault_cli
    plan_dir = tmp_path / "plans"
    plan_id, plan_path = save_plan_from_recipe(tmp_path, plan_dir, GENERATE_RECIPE)
    run("approve-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    monkeypatch.setenv("SECRETOPS_PLAN_DIR", str(plan_dir))
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    real_check = plan_lifecycle._require_plan_execution_allowed

    def check_then_tamper(plan_artifact, console):
        real_check(plan_artifact, console)
        tampered = read_plan(plan_path)
        tampered["secret_name"] = "TAMPERED_SECRET"
        tampered["env"] = "staging"
        plan_path.write_text(json.dumps(tampered), encoding="utf-8")

    monkeypatch.setattr(plan_lifecycle, "_require_plan_execution_allowed", check_then_tamper)
    plan_lifecycle.execute_saved_plan(plan_id, Console())

    stored = json.loads(state.read_text(encoding="utf-8"))
    assert list(stored) == ["https://eu.infisical.com|dummy-project|/|dev"]
    assert list(stored["https://eu.infisical.com|dummy-project|/|dev"]) == ["GENERATED_DUMMY_PASSWORD"]
    final = read_plan(plan_path)
    assert final["status"] == "executed"
    assert final["secret_name"] == "GENERATED_DUMMY_PASSWORD"
    assert final["env"] == "dev"


# 14: atomic plan writes


def test_interrupted_plan_write_keeps_the_old_file(tmp_path, monkeypatch, vault_cli):
    plan_dir = tmp_path / "plans"
    plan_id, plan_path = save_plan_from_recipe(tmp_path, plan_dir, GENERATE_RECIPE)
    original = plan_path.read_bytes()
    monkeypatch.setenv("SECRETOPS_PLAN_DIR", str(plan_dir))
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    def fail_replace(source, destination):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(plans.os, "replace", fail_replace)
    with pytest.raises(OSError):
        plan_lifecycle.transition_planned_plan(plan_id, "approved", "approved", Console())

    assert plan_path.read_bytes() == original
    assert sorted(path.name for path in plan_dir.iterdir()) == [plan_path.name]


# 15, 16: GitHub destination


def test_github_calls_use_the_planned_host_whatever_gh_host_says(tmp_path, monkeypatch):
    for name in ("SECRETOPS_INFISICAL_PROJECT_ID", "SECRETOPS_INFISICAL_PATH", "INFISICAL_TOKEN"):
        monkeypatch.delenv(name)
    bin_dir, gh_calls = write_recording_gh(tmp_path)
    plan_dir = tmp_path / "plans"
    plan_id, plan_path = save_plan_from_recipe(tmp_path, plan_dir, VERIFY_MANY_RECIPE)
    assert read_plan(plan_path)["target"]["target_host"] == "github.com"
    assert "vault_destination" not in read_plan(plan_path)
    monkeypatch.setenv("GH_HOST", "evil.example.com")
    monkeypatch.setenv("GH_REPO", "evil-owner/evil-repo")

    result = run("execute-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir, path_prefix=bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    call = json.loads(gh_calls.read_text(encoding="utf-8").splitlines()[0])
    assert call["args"] == ["secret", "list", "--repo", "github.com/example-owner/secretops", "--env", "staging"]
    assert call["gh_vars"] == []


def test_recipe_github_host_is_recorded_and_used(tmp_path):
    bin_dir, gh_calls = write_recording_gh(tmp_path)
    plan_dir = tmp_path / "plans"
    plan_id, plan_path = save_plan_from_recipe(
        tmp_path, plan_dir, "github_host: ghe.example.com\n" + VERIFY_MANY_RECIPE
    )

    show = run("show-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir)
    result = run("execute-plan", "--id", plan_id, tmp_path=tmp_path, plan_dir=plan_dir, path_prefix=bin_dir)

    assert read_plan(plan_path)["target"]["target_host"] == "ghe.example.com"
    assert "target_host: ghe.example.com" in show.stdout
    assert result.returncode == 0
    call = json.loads(gh_calls.read_text(encoding="utf-8").splitlines()[0])
    assert call["args"][3] == "ghe.example.com/example-owner/secretops"


@pytest.mark.parametrize("host", ["GitHub.com", "github.com:443", "https://github.com", "-github.com", "github.com/evil"])
def test_invalid_github_host_is_rejected_at_planning(tmp_path, host):
    plan_dir = tmp_path / "plans"
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(f"github_host: '{host}'\n" + VERIFY_MANY_RECIPE, encoding="utf-8")

    result = run_secretops("plan", "--file", str(recipe), "--save", plan_dir=plan_dir)

    assert result.returncode != 0
    assert not plan_dir.exists() or list(plan_dir.iterdir()) == []


# Review follow-up: recorded destination and per-action shape are validated
# when the plan is loaded, before show, approval, or any provider call.


def _write_raw_plan(plan_dir, plan_id, data):
    plan_dir.mkdir(exist_ok=True)
    path = plan_dir / f"{plan_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "destination_change",
    [
        {"domain": "https://user:SENSITIVE_MARKER@vault.example.com"},
        {"domain": "HTTPS://Vault.Example.com/"},
        {"domain": "https://vault.example.com/?SENSITIVE_MARKER"},
        {"backend": "openbao"},
        {"path": "relative/path"},
        {"project_id": ""},
        {"project_id": " padded "},
    ],
)
def test_invalid_recorded_destination_is_refused_everywhere(tmp_path, vault_cli, destination_change):
    calls, _ = vault_cli
    plan_dir = tmp_path / "plans"
    plan = json.loads(_approved_generate_text())
    plan["vault_destination"].update(destination_change)
    plan_path = _write_raw_plan(plan_dir, "strict-plan", plan)
    original = plan_path.read_bytes()

    for command in ("show-plan", "approve-plan", "preflight-plan", "execute-plan"):
        result = run(command, "--id", "strict-plan", tmp_path=tmp_path, plan_dir=plan_dir)
        output = result.stdout + result.stderr
        assert result.returncode != 0, (command, output)
        assert "plan file does not match the plan schema" in output
        assert "SENSITIVE_MARKER" not in output
        assert "Traceback" not in output

    assert plan_path.read_bytes() == original
    assert provider_calls(calls) == []
    assert "SENSITIVE_MARKER" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def _copy_many_plan():
    return {
        "plan_id": "shape-plan",
        "action": "vault.secret.copy_many",
        "env": "dev",
        "source_env": "dev",
        "target_env": "staging",
        "status": "planned",
        "secret_value_included": False,
        "create_mode": "create_only",
        "secret_mappings": [{"source": "BACKUP_S3_BUCKET", "target": "BACKUP_S3_BUCKET"}],
        "contract_version": 1,
        "vault_destination": {
            "backend": "infisical",
            "domain": "https://eu.infisical.com",
            "project_id": "dummy-project",
            "path": "/",
        },
    }


def _verify_many_plan():
    return {
        "plan_id": "shape-plan",
        "action": "github.env_secret.verify_many",
        "env": "staging",
        "status": "planned",
        "secret_value_included": False,
        "secret_mappings": [{"name": "BACKUP_S3_BUCKET"}],
        "contract_version": 1,
        "target": {
            "target_host": "github.com",
            "target_owner": "example-owner",
            "target_repo": "secretops",
            "target_environment": "staging",
            "api_call_made": False,
            "secret_value_printed": False,
        },
    }


def _without(key):
    return lambda plan: plan.pop(key)


SHAPE_CASES = {
    "copy_many_name_mapping": (_copy_many_plan, lambda plan: plan.update(secret_mappings=[{"name": "BACKUP_S3_BUCKET"}])),
    "copy_many_mixed_mapping": (
        _copy_many_plan,
        lambda plan: plan["secret_mappings"].append({"name": "BACKUP_AWS_REGION"}),
    ),
    "copy_many_extra_mapping_key": (
        _copy_many_plan,
        lambda plan: plan["secret_mappings"][0].update(name="BACKUP_S3_BUCKET"),
    ),
    "copy_many_without_target_env": (_copy_many_plan, _without("target_env")),
    "copy_many_without_source_env": (_copy_many_plan, _without("source_env")),
    "copy_many_without_create_mode": (_copy_many_plan, _without("create_mode")),
    "copy_many_without_mappings": (_copy_many_plan, _without("secret_mappings")),
    "copy_many_empty_mappings": (_copy_many_plan, lambda plan: plan.update(secret_mappings=[])),
    "copy_many_without_destination": (_copy_many_plan, _without("vault_destination")),
    "verify_many_source_target_mapping": (
        _verify_many_plan,
        lambda plan: plan.update(secret_mappings=[{"source": "BACKUP_S3_BUCKET", "target": "BACKUP_S3_BUCKET"}]),
    ),
    "verify_many_without_target": (_verify_many_plan, _without("target")),
    "verify_many_with_destination": (
        _verify_many_plan,
        lambda plan: plan.update(vault_destination=_copy_many_plan()["vault_destination"]),
    ),
    "generate_without_generator_type": (
        lambda: json.loads(_approved_generate_text()),
        lambda plan: plan["generator"].pop("type"),
    ),
    "generate_without_secret_name": (lambda: json.loads(_approved_generate_text()), _without("secret_name")),
    "generate_without_create_mode": (lambda: json.loads(_approved_generate_text()), _without("create_mode")),
}


@pytest.mark.parametrize("case", sorted(SHAPE_CASES))
def test_action_shape_violations_are_refused_before_provider_calls(tmp_path, vault_cli, case):
    calls, _ = vault_cli
    make_plan, change = SHAPE_CASES[case]
    plan = make_plan()
    change(plan)
    plan["plan_id"] = "shape-plan"
    plan_dir = tmp_path / "plans"
    plan_path = _write_raw_plan(plan_dir, "shape-plan", plan)
    original = plan_path.read_bytes()
    bin_dir, gh_calls = write_recording_gh(tmp_path)

    for command in ("show-plan", "approve-plan", "preflight-plan", "execute-plan"):
        result = run(command, "--id", "shape-plan", tmp_path=tmp_path, plan_dir=plan_dir, path_prefix=bin_dir)
        output = result.stdout + result.stderr
        assert result.returncode != 0, (command, output)
        assert "plan file does not match the plan schema" in output
        assert "Traceback" not in output

    assert plan_path.read_bytes() == original
    assert provider_calls(calls) == []
    assert not gh_calls.exists()


@pytest.mark.parametrize("make_plan", [_copy_many_plan, _verify_many_plan])
def test_action_shape_baselines_are_valid(make_plan):
    validate_plan_content(make_plan(), "shape-plan")


def test_copy_many_recipe_without_target_env_is_not_saved(tmp_path, vault_cli):
    plan_dir = tmp_path / "plans"
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(
        "action: vault.secret.copy_many\n"
        "source_env: dev\n"
        "secrets:\n"
        "  - source: BACKUP_S3_BUCKET\n"
        "    target: BACKUP_S3_BUCKET\n",
        encoding="utf-8",
    )

    result = run_secretops("plan", "--file", str(recipe), "--save", plan_dir=plan_dir)

    assert result.returncode != 0
    assert "no plan was saved" in result.stdout
    assert not plan_dir.exists() or list(plan_dir.iterdir()) == []
