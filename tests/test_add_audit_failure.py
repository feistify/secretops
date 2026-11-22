"""secretops add when the audit log or the plan store fails.

Contract: no audit, no change. A prepare, review or submit step whose audit
event cannot be written refuses with a fixed message before it saves a plan,
records an approval or writes to the vault; the plan stays usable. After the
vault write the known outcome is returned, with a warning for each record
that could not be made. The response keeps its fields, so older clients
still read it.
"""

import json
import os

import pytest
from test_add_client import ScriptedTerminal, direct_args, run
from test_plans import read_plan, write_plan
from test_secret_add import ADD_NAME, ADD_VALUE, Broker, add_plan, read_record

from secretops.add_client import EXIT_CREATED, EXIT_NOT_WRITTEN, EXIT_UNKNOWN
from secretops.human_entry import AUDIT_INCOMPLETE, AUDIT_STOP, EXISTS_MESSAGE, STATUS_UNSAVED


@pytest.fixture
def broker(tmp_path):
    return Broker(tmp_path)


def break_audit(broker):
    if broker.audit_path.exists():
        broker.audit_path.unlink()
    broker.audit_path.mkdir()


def fix_audit(broker):
    broker.audit_path.rmdir()


def break_audit_on_set(broker):
    """Code for the fake vault: the audit log fails once the write starts.
    The vault CLI gets no broker environment, so the path is written in."""
    return (
        f"audit = {str(broker.audit_path)!r}\n"
        "if not os.path.isdir(audit):\n"
        "    os.remove(audit)\n"
        "    os.mkdir(audit)"
    )


def test_prepare_saves_no_plan_without_an_audit_record(broker):
    broker.fake_vault()
    break_audit(broker)

    code, response, result = broker.prepare()

    assert code == 1
    assert response["outcome"] == "refused" and response["message"] == AUDIT_STOP
    assert response["plan"] is None
    assert not broker.plan_dir.exists() or list(broker.plan_dir.glob("*.json")) == []
    assert read_record(broker.record_path)["calls"] == []
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("existing", [(), (ADD_NAME,)], ids=["absent", "exists"])
def test_review_without_an_audit_record_never_asks_for_the_value(broker, existing):
    broker.fake_vault(existing=existing)
    path = write_plan(broker.plan_dir, add_plan())
    before = path.read_bytes()
    break_audit(broker)

    code, response, result = broker.endpoint("review-add", "--id", "add-plan")

    assert code == 1
    assert response["outcome"] == "refused" and response["message"] == AUDIT_STOP
    assert response["digest"] is None
    assert path.read_bytes() == before
    assert b"Traceback" not in result.stderr


def test_submit_writes_nothing_without_an_audit_record_and_the_plan_stays_usable(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    break_audit(broker)

    code, response, submitted = broker.submit(plan_id, review["digest"])

    assert code == 1
    assert response == {"protocol": 1, "outcome": "refused", "message": AUDIT_STOP, "plan_id": plan_id}
    assert "set" not in read_record(broker.record_path)["calls"]
    assert read_plan(broker.plan_path(plan_id))["status"] == "planned"

    # Once the audit log works again, the same plan completes.
    fix_audit(broker)
    broker.assert_no_value(submitted)
    _, review, _ = broker.endpoint("review-add", "--id", plan_id)
    code, response, _ = broker.submit(plan_id, review["digest"])
    assert code == 0 and response["outcome"] == "created"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_approval_that_cannot_be_saved_writes_nothing(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    # The lock file exists, so only the status save fails.
    (broker.plan_dir / f".{plan_id}.lock").touch(mode=0o600)
    broker.plan_dir.chmod(0o500)
    try:
        code, response, _ = broker.submit(plan_id, review["digest"])
    finally:
        broker.plan_dir.chmod(0o700)

    assert code == 1
    assert response["outcome"] == "refused"
    assert response["message"].startswith("the approval could not be saved; nothing was written")
    assert "set" not in read_record(broker.record_path)["calls"]
    assert read_plan(broker.plan_path(plan_id))["status"] == "planned"
    steps = [(event["step"], event["outcome"]) for event in broker.audit_events()]
    assert steps[-2:] == [("approve", "approved"), ("submit", "refused")]


def test_created_secret_with_a_broken_audit_log_reports_created_and_warns(broker):
    broker.fake_vault(before_set=break_audit_on_set(broker))
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]

    code, response, submitted = broker.submit(plan_id, review["digest"])

    assert code == 0, (response, submitted.stderr)
    assert response["outcome"] == "created"
    assert response["message"] == f"saved and verified; {AUDIT_INCOMPLETE}"
    assert read_record(broker.record_path)["stored"][ADD_NAME] == ADD_VALUE
    assert read_plan(broker.plan_path(plan_id))["status"] == "executed"
    assert b"Traceback" not in submitted.stderr
    fix_audit(broker)
    broker.assert_no_value(submitted)


@pytest.mark.parametrize(
    "options,outcome",
    [({"set_exit": 1}, "unknown"), ({"existing": (), "store_other": True}, "unknown")],
    ids=["set-failed", "read-back-mismatch"],
)
def test_unknown_outcome_with_a_broken_audit_log_stays_unknown(broker, options, outcome):
    broker.fake_vault(before_set=break_audit_on_set(broker), **options)
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]

    code, response, submitted = broker.submit(plan_id, review["digest"])

    assert code == 1
    assert response["outcome"] == outcome
    assert response["message"].endswith(AUDIT_INCOMPLETE)
    assert read_plan(broker.plan_path(plan_id))["status"] == "failed"
    assert b"Traceback" not in submitted.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_created_secret_whose_status_cannot_be_saved_reports_created_and_warns(broker):
    broker.fake_vault(before_set=f"os.chmod({str(broker.plan_dir)!r}, 0o500)")
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    try:
        code, response, submitted = broker.submit(plan_id, review["digest"])
    finally:
        broker.plan_dir.chmod(0o700)

    assert code == 0, submitted.stderr
    assert response["message"] == f"saved and verified; {STATUS_UNSAVED}"
    assert read_plan(broker.plan_path(plan_id))["status"] == "approved"
    assert broker.audit_events()[-1]["outcome"] == "created"


# The client shows what the broker added to a known outcome.


@pytest.fixture
def client_broker(tmp_path, monkeypatch):
    started = Broker(tmp_path)
    started.fake_vault(before_set=break_audit_on_set(started))
    for key, value in started.env().items():
        monkeypatch.setenv(key, value)
    return started


def test_client_prints_the_warning_after_a_created_secret(client_broker):
    code, lines = run(ScriptedTerminal(), direct_args())

    assert code == EXIT_CREATED
    index = lines.index("Saved and verified.")
    assert lines[index + 1] == AUDIT_INCOMPLETE
    assert all(ADD_VALUE not in line for line in lines)


def test_client_shows_the_refusal_when_the_audit_log_is_broken_before_entry(tmp_path, monkeypatch):
    started = Broker(tmp_path)
    started.fake_vault()
    break_audit(started)
    for key, value in started.env().items():
        monkeypatch.setenv(key, value)
    terminal = ScriptedTerminal()

    code, lines = run(terminal, direct_args())

    assert code == EXIT_NOT_WRITTEN
    assert f"Not saved: {AUDIT_STOP}" in lines
    assert terminal.opened is False


def test_client_unknown_outcome_keeps_its_lines_and_adds_the_warning(tmp_path, monkeypatch):
    started = Broker(tmp_path)
    started.fake_vault(before_set=break_audit_on_set(started), set_exit=1)
    for key, value in started.env().items():
        monkeypatch.setenv(key, value)

    code, lines = run(ScriptedTerminal(), direct_args())

    assert code == EXIT_UNKNOWN
    # Echoed lines come first; the terminal's own lines are appended by run().
    unknown = lines.index("Write outcome is unknown. Do not submit again yet.")
    assert AUDIT_INCOMPLETE in lines[unknown:]


def marker_code(broker, code):
    """Fake-vault code that runs only once the test has created the marker."""
    marker = broker.tmp_path / "marker"
    body = "".join(f"    {line}\n" for line in code.splitlines())
    return f"if os.path.exists({str(marker)!r}):\n{body}", marker


def test_exists_at_submit_with_a_broken_audit_log_reports_exists_and_warns(broker):
    code_text, marker = marker_code(broker, break_audit_on_set(broker))
    broker.fake_vault(before_run=code_text)
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    # Another writer stores the name after the review.
    record = read_record(broker.record_path)
    record["stored"][ADD_NAME] = "someone-else"
    broker.record_path.write_text(json.dumps(record), encoding="utf-8")
    marker.touch()

    code, response, submitted = broker.submit(plan_id, review["digest"])

    assert code == 1
    assert response["outcome"] == "exists"
    assert response["message"] == f"{EXISTS_MESSAGE}; {AUDIT_INCOMPLETE}"
    assert "set" not in read_record(broker.record_path)["calls"]
    assert read_plan(broker.plan_path(plan_id))["status"] == "failed"
    assert b"Traceback" not in submitted.stderr


def test_provider_error_after_approval_with_a_broken_audit_log_warns(broker):
    code_text, marker = marker_code(broker, break_audit_on_set(broker) + "\nraise SystemExit(1)")
    broker.fake_vault(before_run=code_text)
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    marker.touch()

    code, response, submitted = broker.submit(plan_id, review["digest"])

    assert code == 1
    assert response["outcome"] == "provider_error"
    assert response["message"].endswith(AUDIT_INCOMPLETE)
    assert "set" not in read_record(broker.record_path)["calls"]
    assert read_plan(broker.plan_path(plan_id))["status"] == "failed"
    assert b"Traceback" not in submitted.stderr


def test_review_provider_error_without_an_audit_record_is_refused(broker):
    broker.fake_vault(before_run="raise SystemExit(1)")
    write_plan(broker.plan_dir, add_plan())
    break_audit(broker)

    code, response, _ = broker.endpoint("review-add", "--id", "add-plan")

    assert code == 1
    assert response["outcome"] == "refused" and response["message"] == AUDIT_STOP
    # Control: with a working audit log the same review is a provider error,
    # so the refusal above came from that branch's record.
    fix_audit(broker)
    _, control, _ = broker.endpoint("review-add", "--id", "add-plan")
    assert control["outcome"] == "provider_error"


def test_submit_refusal_without_an_audit_record_is_still_a_refusal(broker):
    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    break_audit(broker)

    # A well-formed digest that does not match the plan: "plan changed after review".
    other = review["digest"][:-1] + ("0" if review["digest"][-1] != "0" else "1")
    code, response, _ = broker.submit(plan_id, other)

    assert code == 1
    assert response["outcome"] == "refused" and response["message"] == AUDIT_STOP
    assert "set" not in read_record(broker.record_path)["calls"]
    assert read_plan(broker.plan_path(plan_id))["status"] == "planned"
    # Control: with a working audit log the same submission is refused as a
    # changed plan, so the refusal above came from that branch's record.
    fix_audit(broker)
    _, control, _ = broker.submit(plan_id, other)
    assert control["message"] == "plan changed after review; nothing was written"
    approvals = [event for event in broker.audit_events() if event.get("step") == "approve"]
    assert approvals == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_both_records_missing_give_two_warnings_one_line_each(broker, tmp_path, monkeypatch):
    broker.fake_vault(
        before_set=break_audit_on_set(broker) + f"\nos.chmod({str(broker.plan_dir)!r}, 0o500)"
    )
    for key, value in broker.env().items():
        monkeypatch.setenv(key, value)
    try:
        code, lines = run(ScriptedTerminal(), direct_args())
    finally:
        broker.plan_dir.chmod(0o700)

    assert code == EXIT_CREATED
    index = lines.index("Saved and verified.")
    assert lines[index + 1 : index + 3] == [STATUS_UNSAVED, AUDIT_INCOMPLETE]


def test_prepare_whose_review_record_fails_names_the_saved_plan(broker):
    # The planned record succeeds; the review record fails.
    broker.fake_vault(before_run=f"os.remove({str(broker.audit_path)!r})\nos.mkdir({str(broker.audit_path)!r})")

    code, response, _ = broker.prepare()

    assert code == 1
    assert response["outcome"] == "refused"
    [path] = broker.plan_dir.glob("*.json")
    plan_id = path.stem
    assert f"plan {plan_id} was saved" in response["message"]
    assert response["message"].endswith(f"secretops add --plan {plan_id}")
    assert read_plan(path)["status"] == "planned"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_prepare_whose_plan_cannot_be_saved_is_refused(broker):
    broker.fake_vault()
    broker.plan_dir.mkdir()
    broker.plan_dir.chmod(0o500)
    try:
        code, response, result = broker.prepare()
    finally:
        broker.plan_dir.chmod(0o700)

    assert code == 1
    assert response["outcome"] == "refused"
    assert response["message"].startswith("the plan could not be saved; nothing was written")
    assert b"Traceback" not in result.stderr


def test_approval_save_failing_for_any_reason_writes_nothing(broker, monkeypatch, tmp_path):
    import secretops.human_entry as human_entry

    broker.fake_vault()
    _, review, _ = broker.prepare()
    plan_id = review["plan"]["plan_id"]
    for key, value in broker.env().items():
        monkeypatch.setenv(key, value)

    def failing_save(*args, **kwargs):
        raise ValueError("schema drift")

    monkeypatch.setattr(human_entry, "set_plan_status", failing_save)
    response = human_entry._submit_locked(plan_id, review["digest"], "in-process-dummy")

    assert response["outcome"] == "refused"
    assert response["message"].startswith("the approval could not be saved; nothing was written")
    assert "set" not in read_record(broker.record_path)["calls"]
    steps = [(event["step"], event["outcome"]) for event in broker.audit_events()]
    assert steps[-2:] == [("approve", "approved"), ("submit", "refused")]
