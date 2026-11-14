"""write_audit_event refuses details that would replace a core field."""

import ast
import json
from pathlib import Path

import pytest

from secretops.audit import CORE_FIELDS, write_audit_event
from secretops.plan_lifecycle import GITHUB_RESULT_CATEGORIES, _github_destination

SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "secretops"
AUDIT_CALLS = {"write_audit_event", "_try_audit"}
# `**name` unpacks inside details whose keys are checked below.
KNOWN_UNPACKS = {"destination", "categories", "digests"}


def test_core_fields_are_the_fields_every_event_carries(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(audit_path))

    write_audit_event(action="a", secret_name="N", env="dev", status="success", plan_id="p")

    assert set(json.loads(audit_path.read_text(encoding="utf-8"))) == CORE_FIELDS


@pytest.mark.parametrize("field", sorted(CORE_FIELDS))
def test_details_naming_a_core_field_are_refused_before_writing(tmp_path, monkeypatch, field):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(audit_path))

    with pytest.raises(ValueError, match=f"must not replace core fields: {field}$"):
        write_audit_event(
            action="a", secret_name="N", env="dev", status="success",
            details={"step": "x", field: "replaced"},
        )

    assert not audit_path.exists()


def _function_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _detail_keys(node: ast.expr, unpacked: set[str]) -> set[str]:
    """Literal keys of a details expression; names of `**name` unpacks are
    collected for a separate check. Anything else fails the test, so a new
    caller must keep its keys visible here."""
    if isinstance(node, ast.Dict):
        keys: set[str] = set()
        for key, value in zip(node.keys, node.values):
            if key is None:
                keys |= _detail_keys(value, unpacked)
            else:
                assert isinstance(key, ast.Constant) and isinstance(key.value, str), ast.dump(key)
                keys.add(key.value)
        return keys
    if isinstance(node, ast.IfExp):
        return _detail_keys(node.body, unpacked) | _detail_keys(node.orelse, unpacked)
    assert isinstance(node, ast.Name), f"details must be a dict display: {ast.dump(node)}"
    unpacked.add(node.id)
    return set()


def test_no_caller_passes_details_that_replace_a_core_field():
    keys: set[str] = set()
    unpacked: set[str] = set()
    digest_keywords: set[str] = set()
    calls = 0
    for path in SOURCE_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = _function_name(node)
            if name in AUDIT_CALLS:
                calls += 1
                for keyword in node.keywords:
                    if keyword.arg == "details":
                        keys |= _detail_keys(keyword.value, unpacked)
            elif name == "_audit" and path.name == "human_entry.py":
                # human_entry._audit(plan, status, step, outcome, **digests)
                digest_keywords |= {keyword.arg for keyword in node.keywords}

    assert calls > 40
    assert unpacked <= KNOWN_UNPACKS
    keys |= digest_keywords
    # `destination`: the GitHub target keys that _github_destination copies.
    target = {"target_host": "h", "target_owner": "o", "target_repo": "r",
              "target_environment": "e", "status": "not copied"}
    keys |= set(_github_destination(type("Plan", (), {"target": target})()))
    keys |= set(GITHUB_RESULT_CATEGORIES)
    assert "approval_digest" in keys and "failure" in keys
    assert not keys & CORE_FIELDS
