"""Vault write paths when the plan status cannot be saved after the write.

Like the GitHub path, execute-plan reports the write results, warns that
the status could not be updated and tells the human not to run the plan
again: exit 1 after confirmed writes, 3 when an outcome is unknown.
"""

import io

import pytest
import typer
from rich.console import Console
from test_plans import (
    copy_many_plan,
    generate_plan,
    import_file_plan,
    read_plan,
    rotate_plan,
    write_plan,
)

import secretops.plan_lifecycle
from secretops.vaults import WriteOutcome

AUDIT_WARNING = [
    "WARNING the audit record could not be completed",
    "the results above are what this run knows; they may be missing from the audit log",
]
WARNING = [
    "WARNING the plan status could not be updated",
    "do not run this plan again; ask the operator to record its outcome",
]
COPY_NAMES = [
    "BACKUP_AWS_ACCESS_KEY_ID",
    "BACKUP_AWS_SECRET_ACCESS_KEY",
    "BACKUP_S3_BUCKET",
    "BACKUP_AWS_REGION",
]


class FakeVault:
    atomic_create = False

    def __init__(self, existing, outcomes=()):
        self.values = dict(existing)
        self.outcomes = list(outcomes)

    def secret_exists(self, name, env):
        return (name, env) in self.values

    def read_secret_value(self, name, env):
        return self.values.get((name, env))

    def write_secret(self, name, env, value):
        self.values[(name, env)] = value
        return self.outcomes.pop(0) if self.outcomes else WriteOutcome.CONFIRMED


def execute_with_unsaved_status(monkeypatch, tmp_path, plan, vault, *, unwritable_audit=False):
    """Run execute-plan in process. The status save fails, or with
    unwritable_audit the audit log cannot be written and the save works."""
    plan_path = write_plan(tmp_path / "plans", plan)
    audit_path = tmp_path / "audit.jsonl"
    if unwritable_audit:
        audit_path.mkdir()
    monkeypatch.setenv("SECRETOPS_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(secretops.plan_lifecycle, "get_vault", lambda destination: vault)
    attempts = []
    real_status_save = secretops.plan_lifecycle.set_plan_status

    def failing_status_save(plan_artifact, status, **kwargs):
        attempts.append(status)
        if unwritable_audit:
            return real_status_save(plan_artifact, status, **kwargs)
        raise OSError("read-only plan store")

    monkeypatch.setattr(secretops.plan_lifecycle, "set_plan_status", failing_status_save)
    buffer = io.StringIO()
    exit_code = 0
    try:
        secretops.plan_lifecycle.execute_saved_plan(plan["plan_id"], Console(file=buffer, width=200))
    except typer.Exit as error:
        exit_code = error.exit_code
    return exit_code, buffer.getvalue().strip().splitlines(), plan_path, attempts


def import_plan_with_source(tmp_path):
    source = tmp_path / "import.env"
    source.write_text("BACKUP_AWS_ACCESS_KEY_ID=import-value\n", encoding="utf-8")
    source.chmod(0o600)
    return import_file_plan("import-unsaved", status="approved", source_file=str(source))


def cases(tmp_path):
    copy_existing = {(name, "dev"): f"copy-value-{name}" for name in COPY_NAMES}
    return {
        "generate": (
            generate_plan("generate-unsaved", status="approved"),
            FakeVault({}),
            ["OK generated secret stored: GENERATED_DUMMY_PASSWORD"],
            "secret value was not printed",
        ),
        "rotate": (
            rotate_plan("rotate-unsaved", status="approved"),
            FakeVault({("GENERATED_DUMMY_PASSWORD", "dev"): "old-value"}),
            ["OK rotated secret stored: GENERATED_DUMMY_PASSWORD"],
            "secret value was not printed",
        ),
        "import": (
            import_plan_with_source(tmp_path),
            FakeVault({}),
            [
                "OK imported dotenv secrets: 1",
                "environment: staging",
                "secret names: BACKUP_AWS_ACCESS_KEY_ID",
            ],
            "secret values were not printed",
        ),
        "copy": (
            copy_many_plan("copy-unsaved", status="approved"),
            FakeVault(copy_existing),
            ["OK vault secrets copied: 4"],
            "secret values were not printed",
        ),
    }


def assert_no_written_value(lines, vault):
    output = "\n".join(lines)
    for value in vault.values.values():
        assert value not in output


@pytest.mark.parametrize("action", ["generate", "rotate", "import", "copy"])
def test_unsaved_status_after_confirmed_vault_write_exits_1(monkeypatch, tmp_path, action):
    plan, vault, ok_lines, not_printed = cases(tmp_path)[action]

    exit_code, lines, plan_path, attempts = execute_with_unsaved_status(
        monkeypatch, tmp_path, plan, vault
    )

    assert exit_code == 1
    assert lines == [*ok_lines, *WARNING, not_printed]
    assert attempts == ["executed"]
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_written_value(lines, vault)


def test_unsaved_status_after_unknown_vault_write_exits_3(monkeypatch, tmp_path):
    vault = FakeVault({}, outcomes=[WriteOutcome.UNKNOWN])

    exit_code, lines, plan_path, attempts = execute_with_unsaved_status(
        monkeypatch, tmp_path, generate_plan("generate-unknown", status="approved"), vault
    )

    assert exit_code == 3
    assert lines[0] == "FAIL generated secret write outcome unknown: GENERATED_DUMMY_PASSWORD"
    assert lines[-3:] == [*WARNING, "secret value was not printed"]
    assert attempts == ["failed"]
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_written_value(lines, vault)


def test_unsaved_status_after_partial_copy_exits_3(monkeypatch, tmp_path):
    copy_existing = {(name, "dev"): f"copy-value-{name}" for name in COPY_NAMES}
    vault = FakeVault(copy_existing, outcomes=[WriteOutcome.CONFIRMED, WriteOutcome.UNKNOWN])

    exit_code, lines, _, attempts = execute_with_unsaved_status(
        monkeypatch, tmp_path, copy_many_plan("copy-partial", status="approved"), vault
    )

    assert exit_code == 3
    assert "written and confirmed: BACKUP_AWS_ACCESS_KEY_ID" in lines
    assert lines[-3:] == [*WARNING, "secret values were not printed"]
    assert attempts == ["failed"]
    assert_no_written_value(lines, vault)


# The audit log cannot be written: the status is still saved, as on the GitHub path


@pytest.mark.parametrize("action", ["generate", "rotate", "import", "copy"])
def test_unwritable_audit_after_confirmed_vault_write_marks_failed_and_exits_1(
    monkeypatch, tmp_path, action
):
    plan, vault, ok_lines, not_printed = cases(tmp_path)[action]

    exit_code, lines, plan_path, attempts = execute_with_unsaved_status(
        monkeypatch, tmp_path, plan, vault, unwritable_audit=True
    )

    assert exit_code == 1
    assert lines == [*ok_lines, *AUDIT_WARNING, not_printed]
    assert attempts == ["failed"]
    # A failed plan cannot run again, so a confirmed rotation is not repeated.
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_written_value(lines, vault)


def test_unwritable_audit_after_unknown_vault_write_exits_3(monkeypatch, tmp_path):
    vault = FakeVault({("GENERATED_DUMMY_PASSWORD", "dev"): "old"}, outcomes=[WriteOutcome.UNKNOWN])

    exit_code, lines, plan_path, attempts = execute_with_unsaved_status(
        monkeypatch, tmp_path, rotate_plan("rotate-unknown", status="approved"), vault,
        unwritable_audit=True,
    )

    assert exit_code == 3
    assert lines[0] == "FAIL rotated secret write outcome unknown: GENERATED_DUMMY_PASSWORD"
    assert lines[-3:] == [*AUDIT_WARNING, "secret value was not printed"]
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_written_value(lines, vault)


def test_unwritable_audit_after_partial_import_exits_3(monkeypatch, tmp_path):
    vault = FakeVault({}, outcomes=[WriteOutcome.UNKNOWN])

    exit_code, lines, plan_path, _ = execute_with_unsaved_status(
        monkeypatch, tmp_path, import_plan_with_source(tmp_path), vault, unwritable_audit=True
    )

    assert exit_code == 3
    assert lines[-3:] == [*AUDIT_WARNING, "secret values were not printed"]
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_written_value(lines, vault)


def test_audit_and_status_both_failing_after_confirmed_write_exit_1(monkeypatch, tmp_path):
    (tmp_path / "audit.jsonl").mkdir()
    vault = FakeVault({})

    exit_code, lines, plan_path, attempts = execute_with_unsaved_status(
        monkeypatch, tmp_path, generate_plan("generate-both", status="approved"), vault
    )

    assert exit_code == 1
    assert lines == [
        "OK generated secret stored: GENERATED_DUMMY_PASSWORD",
        *AUDIT_WARNING,
        *WARNING,
        "secret value was not printed",
    ]
    assert attempts == ["failed"]
    assert read_plan(plan_path)["status"] == "approved"
