"""pending-plans: the human's read-only list of plans that wait for them."""

import os
import signal
from datetime import datetime, timedelta, timezone

import pytest
from test_plans import (
    TEST_VAULT_DESTINATION,
    copy_many_plan,
    diff_envs_plan,
    generate_plan,
    github_plan,
    run_secretops,
    sync_many_plan,
    vault_plan,
    verify_many_plan,
    write_plan,
)
from typer.testing import CliRunner

import secretops.cli
from secretops.cli import app
from secretops.plan_lifecycle import _age_words, pending_plan_lines
from secretops.plans import PlanArtifact, format_timestamp, load_planned_plans

NOW = datetime(2026, 11, 15, 19, 41, 16, tzinfo=timezone.utc)
BROKER = "sudo -u secretops-broker /usr/local/bin/secretops-broker"
NOTE = "   note: the policy denies this environment; approval will not make it run"


@pytest.fixture
def plan_dir(monkeypatch, tmp_path):
    directory = tmp_path / "plans"
    monkeypatch.setenv("SECRETOPS_PLAN_DIR", str(directory))
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(secretops.cli, "utc_now", lambda: NOW)
    return directory


def at(minutes_ago: int) -> str:
    return format_timestamp(NOW - timedelta(minutes=minutes_ago))


def add_plan(plan_id="add-plan", status="planned", name="NEW_KEY", env="dev"):
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


def timed(plan, minutes_ago):
    plan["planned_at"] = at(minutes_ago)
    return plan


def run_pending():
    result = CliRunner().invoke(app, ["pending-plans"])
    assert result.exception is None or isinstance(result.exception, SystemExit)
    return result


def listed_ids(output: str) -> list[str]:
    return [
        line.split(". ", 1)[1].split()[0]
        for line in output.splitlines()
        if line[:1].isdigit() and ". " in line
    ]


def test_only_planned_plans_that_need_the_human_are_listed(plan_dir):
    waiting = [
        generate_plan("gen-planned"),
        copy_many_plan("copy-planned"),
        sync_many_plan("sync-planned"),
        github_plan("write-planned", action="github.env_secret.write"),
        add_plan("add-planned"),
    ]
    others = [
        generate_plan("gen-approved", status="approved"),
        generate_plan("gen-rejected", status="rejected"),
        generate_plan("gen-executed", status="executed"),
        generate_plan("gen-failed", status="failed"),
        add_plan("add-executed", status="executed"),
        vault_plan("check-planned"),
        diff_envs_plan("diff-planned"),
        verify_many_plan("verify-planned"),
    ]
    for plan in waiting + others:
        write_plan(plan_dir, plan)

    result = run_pending()

    assert result.exit_code == 0
    assert result.output.startswith("Plans waiting for you: 5\n")
    assert sorted(listed_ids(result.output)) == sorted(plan["plan_id"] for plan in waiting)


def test_empty_result(plan_dir):
    plan_dir.mkdir()
    write_plan(plan_dir, vault_plan("check-planned"))

    result = run_pending()

    assert result.exit_code == 0
    assert result.output == "No plans wait for you.\n"


def test_missing_plan_directory_counts_as_empty(plan_dir):
    result = run_pending()

    assert result.exit_code == 0
    assert result.output == "No plans wait for you.\n"
    assert not plan_dir.exists()


def test_approval_plan_lines(plan_dir):
    plan = timed(github_plan("9cf4437d", action="github.env_secret.write"), 4)
    plan["target"]["target_owner"] = "OWNER"
    plan["target"]["target_repo"] = "REPO"
    write_plan(plan_dir, plan)

    result = run_pending()

    assert result.output.splitlines() == [
        "Plans waiting for you: 1",
        "",
        "1. 9cf4437d",
        f"   planned: {at(4)} (4 minutes ago)",
        "   action: github.env_secret.write   needs: approval",
        "   from: vault dev",
        "   to: github.com/OWNER/REPO",
        "   github environment: dev",
        "   name: DUMMY_SECRET",
        f"   review:  {BROKER} show-plan --id 9cf4437d",
        f"   approve: {BROKER} approve-plan --id 9cf4437d",
    ]


def test_human_entry_plan_shows_enter_and_no_approve_line(plan_dir):
    write_plan(plan_dir, timed(add_plan("add-1"), 120))

    result = run_pending()

    lines = result.output.splitlines()
    assert lines[2:] == [
        "1. add-1",
        f"   planned: {at(120)} (2 hours ago)",
        "   action: vault.secret.add   needs: your entry",
        "   in: vault dev",
        "   name: NEW_KEY",
        "   enter:   secretops add --plan add-1",
    ]
    assert "approve" not in result.output
    assert "review" not in result.output


def test_copy_many_shows_both_vault_environments(plan_dir):
    write_plan(plan_dir, copy_many_plan("copy-1"))

    output = run_pending().output

    assert "   from: vault dev\n   to: vault staging\n" in output
    assert "   planned: not recorded (older version)\n" in output


def test_vault_destination_is_never_shown(plan_dir):
    destination = {
        "backend": "infisical",
        "domain": "https://vault.example.test",
        "project_id": "project-marker-1234",
        "path": "/folder-marker",
    }
    for plan in (generate_plan("gen-1"), add_plan("add-1"), copy_many_plan("copy-1")):
        plan["vault_destination"] = dict(destination)
        write_plan(plan_dir, plan)
    default = generate_plan("gen-2")
    write_plan(plan_dir, default)

    output = run_pending().output

    assert "Plans waiting for you: 4" in output
    for value in (*destination.values(), *TEST_VAULT_DESTINATION.values()):
        if value in {"infisical", "/"}:
            continue
        assert value not in output
    assert "folder-marker" not in output
    assert "vault.example.test" not in output


def test_order_oldest_first_and_plans_without_time_before_them(plan_dir):
    write_plan(plan_dir, timed(generate_plan("newest"), 1))
    write_plan(plan_dir, timed(generate_plan("oldest"), 300))
    write_plan(plan_dir, generate_plan("untimed-b"))
    write_plan(plan_dir, timed(add_plan("middle"), 30))
    write_plan(plan_dir, generate_plan("untimed-a"))

    output = run_pending().output

    assert listed_ids(output) == ["untimed-a", "untimed-b", "oldest", "middle", "newest"]


def test_at_most_fifty_newest_plans_are_shown(plan_dir):
    for index in range(53):
        write_plan(plan_dir, timed(generate_plan(f"plan-{index:02d}"), 1000 - index))
    write_plan(plan_dir, generate_plan("untimed"))

    output = run_pending().output

    ids = listed_ids(output)
    assert output.splitlines()[:2] == [
        "Plans waiting for you: 54",
        "4 older plans not shown; the 50 newest follow.",
    ]
    assert len(ids) == 50
    assert ids[0] == "plan-03"
    assert ids[-1] == "plan-52"
    assert "untimed" not in ids


def test_names_are_truncated_after_ten(plan_dir):
    plan = copy_many_plan("copy-1")
    plan["secret_mappings"] = [
        {"source": f"NAME_{index:02d}", "target": f"NAME_{index:02d}"} for index in range(13)
    ]
    plan["secret_mappings"][0]["target"] = "RENAMED"
    write_plan(plan_dir, plan)

    output = run_pending().output

    expected = [
        "   name: NAME_00 -> RENAMED",
        *(f"   name: NAME_{index:02d}" for index in range(1, 10)),
        "   and 3 more names",
    ]
    assert "\n".join(expected) + "\n" in output
    assert "NAME_10" not in output


def test_listing_changes_nothing_and_writes_no_audit(plan_dir, tmp_path):
    paths = [
        write_plan(plan_dir, generate_plan("gen-1")),
        write_plan(plan_dir, add_plan("add-1")),
    ]
    before = {path: path.read_bytes() for path in paths}
    names_before = sorted(os.listdir(plan_dir))

    assert run_pending().exit_code == 0

    assert {path: path.read_bytes() for path in paths} == before
    assert sorted(os.listdir(plan_dir)) == names_before
    assert not (tmp_path / "audit.jsonl").exists()


def test_control_characters_in_plan_fields_cannot_forge_output(plan_dir):
    forged = "dev\n2. forged   action: x\x1b[2J\x1b]0;title\x07\r\u202e\u2028"
    plan = copy_many_plan("copy-1")
    plan["env"] = forged
    plan["source_env"] = forged
    plan["target_env"] = forged
    write_plan(plan_dir, plan)

    output = run_pending().output

    assert "\x1b" not in output and "\x07" not in output and "\r" not in output
    assert "\u202e" not in output and "\u2028" not in output
    lines = output.splitlines()
    # The forged environment is not allowed by policy, so the note is added.
    assert len(lines) == 14
    assert NOTE in lines
    assert lines[5] == "   from: vault dev?2. forged action: x?[2J?]0;title????"
    assert listed_ids(output) == ["copy-1"]


def test_loader_refuses_control_characters_in_names(plan_dir):
    plan = generate_plan("gen-1")
    plan["secret_name"] = "BAD\nNAME"
    write_plan(plan_dir, plan)

    result = run_pending()

    assert result.exit_code == 0
    assert "BAD" not in result.output
    assert result.output == (
        "No plans wait for you.\n\n"
        "1 plan file could not be read; inspect it with show-plan or remove it.\n"
    )


def test_unreadable_files_are_counted_and_other_files_ignored(plan_dir):
    write_plan(plan_dir, generate_plan("good"))
    (plan_dir / "broken.json").write_text('{"plan_id": "MARKER-broken"', encoding="utf-8")
    (plan_dir / "schema.json").write_text(
        '{"plan_id": "schema", "action": "MARKER-schema"}', encoding="utf-8"
    )
    write_plan(plan_dir, generate_plan("inner-id")).rename(plan_dir / "other-id.json")
    (plan_dir / "odd name.json").write_text('{"MARKER": 1}', encoding="utf-8")
    (plan_dir / "legacy.json").write_text(
        '{"plan_id": "legacy", "action": "vault.secret.generate", "env": "dev", '
        '"status": "planned", "secret_value_included": false}',
        encoding="utf-8",
    )
    (plan_dir / ".good.lock").write_text("", encoding="utf-8")
    (plan_dir / ".plan-abc.tmp").write_text("MARKER", encoding="utf-8")
    (plan_dir / "notes.txt").write_text("MARKER", encoding="utf-8")

    result = run_pending()

    assert result.exit_code == 0
    assert listed_ids(result.output) == ["good"]
    assert "MARKER" not in result.output
    assert "Traceback" not in result.output
    assert result.output.endswith(
        "\n5 plan files could not be read; inspect them with show-plan or remove them.\n"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files regardless of mode")
def test_unreadable_plan_file_is_counted(plan_dir):
    write_plan(plan_dir, generate_plan("good"))
    write_plan(plan_dir, generate_plan("locked")).chmod(0)

    plans, unreadable = load_planned_plans()

    assert [plan.plan_id for plan in plans] == ["good"]
    assert unreadable == 1


def test_plan_directory_that_cannot_be_read_fails(plan_dir):
    plan_dir.parent.mkdir(parents=True, exist_ok=True)
    plan_dir.write_text("not a directory", encoding="utf-8")

    result = run_pending()

    assert result.exit_code == 1
    assert result.output == "FAIL plan directory could not be read\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists directories regardless of mode")
def test_plan_directory_without_permission_fails(plan_dir):
    plan_dir.mkdir()
    plan_dir.chmod(0)
    try:
        result = run_pending()
    finally:
        plan_dir.chmod(0o700)

    assert result.exit_code == 1
    assert result.output == "FAIL plan directory could not be read\n"


def test_invalid_policy_stops_the_command(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text("allowed_actions: []\nunknown_key: 1\n", encoding="utf-8")
    plans = tmp_path / "plans"
    write_plan(plans, generate_plan("gen-1"))

    result = run_secretops(
        "pending-plans", plan_dir=plans, audit_path=tmp_path / "audit.jsonl", policy_path=policy
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith(f"FAIL policy file {policy}")


def test_pending_plans_end_to_end(tmp_path):
    plans = tmp_path / "plans"
    write_plan(plans, add_plan("add-1"))

    result = run_secretops("pending-plans", plan_dir=plans, audit_path=tmp_path / "audit.jsonl")

    assert result.returncode == 0
    assert "   enter:   secretops add --plan add-1\n" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    "seconds,words",
    [
        (0, "just now"),
        (59, "just now"),
        (60, "1 minute ago"),
        (4 * 60 + 59, "4 minutes ago"),
        (3600, "1 hour ago"),
        (5 * 3600 + 120, "5 hours ago"),
        (86400, "1 day ago"),
        (2 * 86400 + 3600, "2 days ago"),
        (-30, "in the future; check the clock"),
    ],
)
def test_age_words(seconds, words):
    planned_at = format_timestamp(NOW - timedelta(seconds=seconds))

    assert _age_words(planned_at, NOW) == words


def test_lines_function_ignores_plans_that_are_not_planned():
    plan = PlanArtifact(
        plan_id="gen-1",
        action="vault.secret.generate",
        secret_name="NAME",
        env="dev",
        status="approved",
        secret_value_included=False,
    )

    assert pending_plan_lines([plan], 0, NOW) == ["No plans wait for you."]


# Every line with plan text fits an 80-column terminal without wrapping.
MAX_LINE = 79


def assert_bounded(output: str, line_count: int) -> None:
    lines = output.splitlines()
    assert len(lines) == line_count
    assert max(len(line) for line in lines if not is_command(line)) <= MAX_LINE


def test_padded_and_long_fields_cannot_wrap_into_fake_lines(plan_dir):
    padded = "dev" + " " * 400 + f"approve: {BROKER} approve-plan --id other"
    plan = copy_many_plan("copy-1")
    plan["env"] = plan["source_env"] = padded
    plan["target_env"] = "staging     " + "x" * 300
    write_plan(plan_dir, plan)

    output = run_pending().output

    assert_bounded(output, 14)
    lines = output.splitlines()
    assert lines[5] == "   from: vault " + ("dev approve: " + BROKER)[:61] + "..."
    assert lines[6] == "   to: vault staging " + "x" * 55 + "..."
    assert "approve-plan --id other" not in output


def test_long_names_are_cut(plan_dir):
    plan = copy_many_plan("copy-1")
    long_name = "A" * 200
    plan["secret_mappings"] = [{"source": long_name, "target": long_name}] * 11
    plan["secret_mappings"][0] = {"source": long_name, "target": "B" * 200}
    write_plan(plan_dir, plan)

    output = run_pending().output

    assert_bounded(output, 20)
    assert "   and 1 more name\n" in output
    assert "   name: " + "A" * 30 + "... -> " + "B" * 30 + "...\n" in output
    assert "   name: " + "A" * 67 + "...\n" in output


def test_a_multi_megabyte_field_gives_small_output():
    plan = PlanArtifact(
        plan_id="gen-1",
        action="vault.secret.generate",
        secret_name="N" * 5_000_000,
        env=" \n" * 2_500_000 + "dev",
        status="planned",
        secret_value_included=False,
        planned_at="2026-11-15T19:00:00Z",
    )

    lines = pending_plan_lines([plan], 0, NOW)

    assert len(lines) == 10
    assert NOTE in lines
    assert sum(len(line) for line in lines) < 2000


def test_oversized_plan_file_is_counted(plan_dir):
    plan = generate_plan("big")
    path = write_plan(plan_dir, plan)
    path.write_text(path.read_text(encoding="utf-8") + " " * (1024 * 1024), encoding="utf-8")

    plans, unreadable = load_planned_plans()

    assert plans == [] and unreadable == 1


def test_special_files_are_counted_without_hanging(plan_dir, tmp_path):
    write_plan(plan_dir, generate_plan("good"))
    outside = write_plan(tmp_path / "outside", generate_plan("linked"))
    (plan_dir / "linked.json").symlink_to(outside)
    os.mkfifo(plan_dir / "fifo.json")
    (plan_dir / "folder.json").mkdir()

    def timeout(_signal, _frame):
        raise AssertionError("listing hung on a special file")

    previous = signal.signal(signal.SIGALRM, timeout)
    signal.alarm(5)
    try:
        result = run_pending()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert result.exit_code == 0
    assert listed_ids(result.output) == ["good"]
    assert "linked" not in result.output.replace("could not be read", "")
    assert result.output.endswith(
        "\n3 plan files could not be read; inspect them with show-plan or remove them.\n"
    )


def custom_policy(tmp_path, monkeypatch, change):
    from importlib import resources

    import yaml

    data = yaml.safe_load(
        resources.files("secretops.policies").joinpath("default.yaml").read_text("utf-8")
    )
    change(data)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("SECRETOPS_POLICY_PATH", str(path))


def test_listing_uses_the_lifecycle_approval_rule(plan_dir, tmp_path, monkeypatch):
    # vault.read.check needs approval when the policy says so;
    # vault.secret.generate always needs it (APPROVAL_ALWAYS_REQUIRED);
    # github.env_secret.plan is not in that set, so the policy decides.
    def change(data):
        data["actions"]["vault.read.check"]["requires_approval"] = True
        data["actions"]["vault.secret.generate"]["requires_approval"] = False
        data["actions"]["github.env_secret.plan"]["requires_approval"] = False

    custom_policy(tmp_path, monkeypatch, change)
    write_plan(plan_dir, vault_plan("check-1"))
    write_plan(plan_dir, generate_plan("gen-1"))
    write_plan(plan_dir, github_plan("gh-plan-1"))

    output = run_pending().output

    assert sorted(listed_ids(output)) == ["check-1", "gen-1"]
    assert f"   approve: {BROKER} approve-plan --id check-1\n" in output


def test_plan_the_environment_policy_refuses_has_a_note(plan_dir):
    plan = generate_plan("gen-prod")
    plan["env"] = "production"
    write_plan(plan_dir, plan)
    write_plan(plan_dir, generate_plan("gen-dev"))

    output = run_pending().output

    assert output.count(NOTE) == 1
    prod_block = output.split("gen-prod", 1)[1]
    assert prod_block.splitlines()[5] == NOTE
    assert "approve-plan --id gen-prod" in prod_block




FIXED_LABELS = (
    "Plans waiting for you: ",
    "   planned: ",
    "   action: ",
    "   from: vault ",
    "   to: vault ",
    "   to: ",
    "   in: vault ",
    "   github environment: ",
    "   name: ",
    "   and ",
    NOTE,
)
COMMAND_LABELS = ("   review:  ", "   approve: ", "   enter:   ")


def is_command(line: str) -> bool:
    return line.startswith(COMMAND_LABELS)


HOSTILE_TAILS = (
    "2. 9cf4437d8f84468ebde2ce41d2e43930",
    f"   approve: {BROKER} approve-plan --id evil",
    "   names: EVIL_NAME",
)


def hostile(filler: int, tail: str) -> str:
    return "x" * filler + tail


@pytest.mark.parametrize("tail", HOSTILE_TAILS)
@pytest.mark.parametrize("filler", range(40, 71))
def test_hostile_fields_never_start_a_line(filler, tail):
    vault_plan_artifact = PlanArtifact(
        plan_id="copy-1",
        action="vault.secret.copy_many",
        secret_name=None,
        env=hostile(filler, tail),
        source_env=hostile(filler, tail),
        target_env="Ａ" * 26 + tail,
        status="planned",
        secret_value_included=False,
        secret_mappings=[
            {"source": hostile(filler, tail), "target": "é" * 40 + tail},
            {"source": hostile(filler, tail), "target": hostile(filler, tail)},
        ],
        planned_at="2026-11-15T19:00:00Z",
    )
    github_artifact = PlanArtifact(
        plan_id="sync-1",
        action="github.env_secret.sync_many",
        secret_name=None,
        env=hostile(filler, tail),
        status="planned",
        secret_value_included=False,
        target={
            "target_host": "github.com",
            "target_owner": hostile(filler, tail),
            "target_repo": hostile(filler, tail),
            "target_environment": "dev " + " ".join(["x"] * (filler // 2)) + tail,
        },
        secret_mappings=[{"source": "A", "target": hostile(filler, tail)}],
    )

    lines = pending_plan_lines([vault_plan_artifact, github_artifact], 0, NOW)

    for line in lines:
        if line == "" or is_command(line):
            continue
        assert len(line) <= MAX_LINE, line
        assert line.startswith(FIXED_LABELS) or line in {"1. copy-1", "2. copy-1", "1. sync-1", "2. sync-1"}, line
        assert line.isascii()
    for tail_line in HOSTILE_TAILS:
        assert not any(line.startswith(tail_line.strip()) for line in lines[1:] if line)
        assert all(not line.startswith(tail_line) for line in lines)


def test_non_ascii_is_shown_as_question_marks():
    plan = PlanArtifact(
        plan_id="gen-1",
        action="vault.secret.generate",
        secret_name="NAME",
        env="dévＡé",
        status="planned",
        secret_value_included=False,
    )

    lines = pending_plan_lines([plan], 0, NOW)

    assert "   in: vault d?v?e?" in lines
