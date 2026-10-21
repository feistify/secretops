"""Broker endpoints for human secret entry (`vault.secret.add`).

The human client (`secretops add`) calls these through the human broker
access: `prepare-add` or `review-add` return the broker's validated plan
metadata and digest, and `submit-add` receives the reviewed digest and the
value in one bounded JSON request on stdin. Requests and responses are
versioned JSON; responses carry metadata only, never the value.

These endpoints are not in the agent wrapper allowlist. The channel label
"human" in audit events is set here, by the endpoint, never by the request.
"""

import json
import os
import re
import select
import time
import unicodedata
from typing import Any

from secretops.audit import write_audit_event
from secretops.plan_lifecycle import _binding_failures
from secretops.plans import (
    DIGEST_PREFIX,
    HUMAN_ENTRY_ACTIONS,
    HUMAN_PROMPT_SOURCE,
    PLAN_ID_PATTERN,
    PlanBusyError,
    PlanFileError,
    build_plan_artifact,
    load_plan,
    parse_plan_json,
    plan_digest,
    plan_lock,
    save_plan_artifact,
    set_plan_status,
)
from secretops.policy import require_environment_allowed
from secretops.recipes import Recipe
from secretops.validation import validate_secret_name
from secretops.vaults import (
    CreateOutcome,
    VaultError,
    configured_vault_destination,
    get_vault,
)

ACTION = "vault.secret.add"
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_VALUE_BYTES = 16 * 1024
RECEIVE_DEADLINE_SECONDS = 120
CHANNEL = "human"
ENV_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
REQUEST_FIELDS = {"protocol": int, "plan_id": str, "digest": str, "value": str}
# Line breaks other than CR/LF that a terminal or provider may treat as one.
LINE_SEPARATORS = {"Zl", "Zp"}

REVIEW_OUTCOMES = ("ready", "exists", "refused", "provider_error")
SUBMIT_OUTCOMES = ("created", "exists", "refused", "provider_error", "unknown")


class InvalidValueError(ValueError):
    """The entered value cannot be stored. The message never contains it."""


class RequestError(Exception):
    """The submission request is unusable; nothing was written."""


def validate_entered_value(value: Any) -> str:
    """Return the value unchanged, or raise InvalidValueError.

    One nonblank line of at most MAX_VALUE_BYTES UTF-8 bytes, without control
    characters or line separators. Spaces and Unicode are kept exactly; there
    is no trimming or normalization.
    """
    if not isinstance(value, str):
        raise InvalidValueError("the value must be text")
    if not value.strip():
        raise InvalidValueError("the value is empty or contains only whitespace")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidValueError("the value is not valid Unicode text") from error
    if len(encoded) > MAX_VALUE_BYTES:
        raise InvalidValueError(f"the value is longer than {MAX_VALUE_BYTES} bytes")
    for character in value:
        category = unicodedata.category(character)
        if character in "\r\n" or category in LINE_SEPARATORS:
            raise InvalidValueError("the value must be a single line")
        if category in {"Cc", "Cs"}:
            raise InvalidValueError("the value contains a control character")
    return value


def plan_review_metadata(plan_artifact) -> dict[str, str]:
    destination = plan_artifact.vault_destination or {}
    return {
        "plan_id": plan_artifact.plan_id,
        "secret_name": plan_artifact.secret_name or "",
        "env": plan_artifact.env,
        "create_mode": plan_artifact.create_mode or "",
        "value_source": plan_artifact.value_source or "",
        "vault_backend": destination.get("backend", ""),
        "vault_domain": destination.get("domain", ""),
        "vault_project_id": destination.get("project_id", ""),
        "vault_path": destination.get("path", ""),
    }


def _review_response(outcome: str, message: str, plan_artifact=None, **extra) -> dict[str, Any]:
    response = {
        "protocol": PROTOCOL_VERSION,
        "outcome": outcome,
        "message": message,
        "plan": plan_review_metadata(plan_artifact) if plan_artifact is not None else None,
        "digest": None,
        "atomic_create": None,
    }
    response.update(extra)
    return response


def _submit_response(outcome: str, message: str, plan_id: str | None) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "outcome": outcome,
        "message": message,
        "plan_id": plan_id,
    }


def _audit(plan_artifact, status: str, step: str, outcome: str, **digests: str) -> None:
    # digests: reviewed_digest and/or approval_digest. They cover metadata
    # only; nothing derived from the value is recorded.
    write_audit_event(
        action=ACTION,
        secret_name=plan_artifact.secret_name or "unknown",
        env=plan_artifact.env,
        status=status,
        plan_id=plan_artifact.plan_id,
        details={"channel": CHANNEL, "step": step, "outcome": outcome, **digests},
    )


def _load_add_plan(plan_id: str):
    """Load a planned vault.secret.add plan, or return (None, refusal message)."""
    if not isinstance(plan_id, str) or not PLAN_ID_PATTERN.fullmatch(plan_id):
        return None, "plan id is not valid"
    try:
        plan_artifact = load_plan(plan_id)
    except PlanFileError as error:
        return None, str(error)
    if plan_artifact.action not in HUMAN_ENTRY_ACTIONS:
        return None, f"plan is not a {ACTION} plan"
    if plan_artifact.status != "planned":
        return None, f"plan status is {plan_artifact.status}; a plan is completed only once"
    return plan_artifact, None


def _plan_refusal(plan_artifact) -> str | None:
    """Destination binding and environment policy, without provider calls.

    Raises VaultError when the configured destination cannot be resolved.
    """
    failures = _binding_failures(plan_artifact)
    if failures:
        return failures[0]
    try:
        require_environment_allowed(ACTION, plan_artifact.env)
    except PermissionError as error:
        return str(error)
    return None


def review_add(plan_id: str) -> dict[str, Any]:
    """Validate a saved plan and check that the secret is absent.

    Does not change the plan. Returns the broker's metadata and digest that
    the human confirms.
    """
    plan_artifact, refusal = _load_add_plan(plan_id)
    if plan_artifact is None:
        return _review_response("refused", refusal)
    try:
        refusal = _plan_refusal(plan_artifact)
        if refusal:
            _audit(plan_artifact, "failure", "review", "refused")
            return _review_response("refused", refusal, plan_artifact)
        vault = get_vault(plan_artifact.vault_destination)
        exists = vault.secret_exists(plan_artifact.secret_name, plan_artifact.env)
    except VaultError as error:
        _audit(plan_artifact, "failure", "review", "provider_error")
        return _review_response("provider_error", f"vault error: {error}", plan_artifact)
    if exists:
        _audit(plan_artifact, "failure", "review", "exists")
        return _review_response(
            "exists", "secret already exists; nothing was written", plan_artifact
        )
    digest = plan_digest(plan_artifact.loaded_content)
    _audit(plan_artifact, "success", "review", "ready", reviewed_digest=digest)
    return _review_response(
        "ready",
        "checks passed; the secret is currently absent",
        plan_artifact,
        digest=digest,
        atomic_create=bool(vault.atomic_create),
    )


def prepare_add(name: str, env: str) -> dict[str, Any]:
    """Save a new vault.secret.add plan through normal planning, then review it."""
    try:
        name = validate_secret_name(name)
    except ValueError as error:
        return _review_response("refused", str(error))
    if not isinstance(env, str) or not ENV_PATTERN.fullmatch(env):
        return _review_response("refused", "environment name is not valid")
    try:
        # Policy first: a denied environment saves no plan and calls no provider.
        require_environment_allowed(ACTION, env)
    except PermissionError as error:
        return _review_response("refused", str(error))
    try:
        destination = configured_vault_destination()
    except VaultError as error:
        return _review_response("provider_error", f"vault destination is not configured: {error}")
    recipe = Recipe(
        action=ACTION,
        secret_name=name,
        env=env,
        create_mode="create_only",
        value_source=HUMAN_PROMPT_SOURCE,
    )
    plan_artifact = build_plan_artifact(recipe, destination)
    save_plan_artifact(plan_artifact)
    write_audit_event(
        action=ACTION,
        secret_name=name,
        env=env,
        status="planned",
        plan_id=plan_artifact.plan_id,
        details={"channel": CHANNEL, "step": "prepare", "outcome": "planned"},
    )
    return review_add(plan_artifact.plan_id)


def read_request(fd: int, *, limit: int = MAX_REQUEST_BYTES,
                 deadline_seconds: float = RECEIVE_DEADLINE_SECONDS) -> bytes:
    """Read one request until EOF, refusing oversize or late input."""
    deadline = time.monotonic() + deadline_seconds
    chunks: list[bytes] = []
    size = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestError("the request did not complete in time")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise RequestError("the request did not complete in time")
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise RequestError("the request is too large")
        chunks.append(chunk)


def parse_submit_request(raw: bytes) -> dict[str, Any]:
    """Strictly parse a submission request. Messages never include the value."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RequestError("the request is not valid UTF-8") from error
    if not text.endswith("\n") or "\n" in text[:-1]:
        raise RequestError("the request must be one JSON line")
    try:
        data = parse_plan_json(text[:-1])
    except ValueError as error:
        raise RequestError("the request is not valid JSON") from error
    if not isinstance(data, dict) or set(data) != set(REQUEST_FIELDS):
        raise RequestError("the request fields are not valid")
    for key, expected in REQUEST_FIELDS.items():
        value = data[key]
        if expected is int and type(value) is not int:
            raise RequestError("the request fields are not valid")
        if expected is str and not isinstance(value, str):
            raise RequestError("the request fields are not valid")
    if data["protocol"] != PROTOCOL_VERSION:
        raise RequestError("the request protocol version is not supported")
    if not data["digest"].startswith(DIGEST_PREFIX):
        raise RequestError("the reviewed digest is not valid")
    return data


def submit_add(raw: bytes) -> dict[str, Any]:
    """Approve and execute one reviewed plan with the submitted value."""
    try:
        request = parse_submit_request(raw)
    except RequestError as error:
        return _submit_response("refused", str(error), None)
    plan_id = request["plan_id"]
    if not PLAN_ID_PATTERN.fullmatch(plan_id):
        return _submit_response("refused", "plan id is not valid", None)
    try:
        value = validate_entered_value(request["value"])
    except InvalidValueError as error:
        return _submit_response("refused", f"invalid value: {error}", plan_id)
    try:
        with plan_lock(plan_id):
            return _submit_locked(plan_id, request["digest"], value)
    except PlanBusyError:
        return _submit_response("refused", "plan is in use by another command", plan_id)


def _submit_locked(plan_id: str, reviewed_digest: str, value: str) -> dict[str, Any]:
    plan_artifact, refusal = _load_add_plan(plan_id)
    if plan_artifact is None:
        return _submit_response("refused", refusal, plan_id)
    if plan_digest(plan_artifact.loaded_content) != reviewed_digest:
        _audit(plan_artifact, "failure", "submit", "refused", reviewed_digest=reviewed_digest)
        return _submit_response(
            "refused", "plan changed after review; nothing was written", plan_id
        )
    try:
        refusal = _plan_refusal(plan_artifact)
    except VaultError as error:
        _audit(plan_artifact, "failure", "submit", "provider_error", reviewed_digest=reviewed_digest)
        return _submit_response("provider_error", f"vault error: {error}", plan_id)
    if refusal:
        _audit(plan_artifact, "failure", "submit", "refused", reviewed_digest=reviewed_digest)
        return _submit_response("refused", refusal, plan_id)

    # The human confirmed this digest; approval is recorded with it here,
    # on the authenticated human path, and executed with the same object.
    plan_artifact = set_plan_status(plan_artifact, "approved", approval_digest=reviewed_digest)
    _audit(
        plan_artifact, "success", "approve", "approved",
        reviewed_digest=reviewed_digest, approval_digest=reviewed_digest,
    )
    try:
        vault = get_vault(plan_artifact.vault_destination)
        outcome = vault.create_secret(plan_artifact.secret_name, plan_artifact.env, value)
    except VaultError as error:
        set_plan_status(plan_artifact, "failed")
        _audit(plan_artifact, "failure", "submit", "provider_error", reviewed_digest=reviewed_digest)
        return _submit_response("provider_error", f"vault error: {error}", plan_id)
    except Exception:  # noqa: BLE001 - an unexpected failure may follow a write
        set_plan_status(plan_artifact, "failed")
        _audit(plan_artifact, "outcome_unknown", "submit", "unknown", reviewed_digest=reviewed_digest)
        return _submit_response(
            "unknown", "write outcome is unknown; check the secret before any new attempt", plan_id
        )
    if outcome is CreateOutcome.CONFIRMED:
        set_plan_status(plan_artifact, "executed")
        _audit(plan_artifact, "success", "submit", "created", reviewed_digest=reviewed_digest)
        return _submit_response("created", "saved and verified", plan_id)
    set_plan_status(plan_artifact, "failed")
    if outcome is CreateOutcome.EXISTS:
        _audit(plan_artifact, "failure", "submit", "exists", reviewed_digest=reviewed_digest)
        return _submit_response(
            "exists", "secret already exists; nothing was written by this invocation", plan_id
        )
    _audit(plan_artifact, "outcome_unknown", "submit", "unknown", reviewed_digest=reviewed_digest)
    return _submit_response(
        "unknown", "write outcome is unknown; check the secret before any new attempt", plan_id
    )


def response_line(response: dict[str, Any]) -> str:
    return json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n"
