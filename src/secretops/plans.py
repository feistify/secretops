import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from secretops.import_file import parse_import_source_file
from secretops.targets import GitHubEnvSecretTarget
from secretops.validation import (
    DEFAULT_GITHUB_HOST,
    validate_github_host,
    validate_github_target_metadata,
    validate_plan_secret_fields,
    validate_secret_name,
)
from secretops.vaults import InfisicalConnection, VaultError

DEFAULT_PLAN_DIR = Path(".plans")
PLAN_DIR_ENV = "SECRETOPS_PLAN_DIR"
PLAN_STATUSES = {"planned", "approved", "rejected", "executed", "failed"}

# Version of what an approved plan means. Increment it for any change that
# alters what an existing plan does, including behaviour changes that keep
# the JSON structure; plans with another version must be replanned.
CONTRACT_VERSION = 1
DIGEST_PREFIX = "sha256-v1:"
DIGEST_EXCLUDED_FIELDS = ("status", "approval_digest")

# Actions that read or write the vault and therefore record its destination.
VAULT_ACTIONS = frozenset(
    {
        "vault.read.check",
        "vault.secret.generate",
        "vault.secret.rotate",
        "vault.secret.import_file",
        "vault.secret.copy_many",
        "vault.secret.diff_envs",
        "vault.secret.add",
        "github.env_secret.write",
        "github.env_secret.sync_many",
    }
)
# Actions whose value is entered by a human during submission: never approved
# through approve-plan and never executed through execute-plan.
HUMAN_ENTRY_ACTIONS = frozenset({"vault.secret.add"})
HUMAN_PROMPT_SOURCE = "human_prompt"
GITHUB_ACTIONS = frozenset(
    {
        "github.env_secret.plan",
        "github.env_secret.write",
        "github.env_secret.sync_many",
        "github.env_secret.verify_many",
    }
)
GITHUB_SINGLE_SECRET_ACTIONS = frozenset({"github.env_secret.plan", "github.env_secret.write"})

# Contract version 1 plan schema: one JSON type per field, and per action the
# optional fields that may appear. Anything else is rejected, not ignored.
REQUIRED_FIELD_TYPES = {
    "plan_id": str,
    "action": str,
    "env": str,
    "status": str,
    "secret_value_included": bool,
    "contract_version": int,
}
OPTIONAL_FIELD_TYPES = {
    "secret_name": str,
    "source_env": str,
    "target_env": str,
    "target": dict,
    "secret_mappings": list,
    "generator": dict,
    "create_mode": str,
    "rotation_mode": str,
    "source_file": str,
    "value_source": str,
    "vault_destination": dict,
    "approval_digest": str,
}
# Per action: fields that must be present, fields that may be present, and
# the shape of secret_mappings entries ("name" or "source_target").
ACTION_SCHEMAS = {
    "vault.read.check": ({"secret_name"}, set(), None),
    "vault.secret.generate": ({"secret_name", "generator", "create_mode"}, set(), None),
    "vault.secret.rotate": ({"secret_name", "generator", "rotation_mode"}, set(), None),
    "vault.secret.import_file": (
        {"source_file", "create_mode", "secret_mappings"},
        {"secret_name"},
        "name",
    ),
    "vault.secret.copy_many": (
        {"source_env", "target_env", "create_mode", "secret_mappings"},
        {"secret_name"},
        "source_target",
    ),
    "vault.secret.diff_envs": (
        {"source_env", "target_env", "secret_mappings"},
        {"secret_name"},
        "name",
    ),
    "vault.secret.add": ({"secret_name", "create_mode", "value_source"}, set(), None),
    "github.env_secret.plan": ({"secret_name", "target"}, set(), None),
    "github.env_secret.write": ({"secret_name", "target"}, set(), None),
    "github.env_secret.sync_many": ({"target", "secret_mappings"}, {"secret_name"}, "source_target"),
    "github.env_secret.verify_many": ({"target", "secret_mappings"}, {"secret_name"}, "name"),
}
MAPPING_FIELDS = {"name": {"name"}, "source_target": {"source", "target"}}
GENERATOR_FIELD_TYPES = {"type": str, "length": int}
GENERATOR_REQUIRED_FIELDS = {"type"}
VAULT_DESTINATION_FIELDS = ("backend", "domain", "project_id", "path")
GITHUB_TARGET_FIELD_TYPES = {
    "target_host": str,
    "target_owner": str,
    "target_repo": str,
    "target_environment": str,
    "api_call_made": bool,
    "secret_value_printed": bool,
}


PLAN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


class PlanBusyError(Exception):
    """Another process holds the completion lock for this plan."""


class PlanFileError(Exception):
    """The plan file cannot be trusted, so nothing in it is used.

    `category` is a fixed label that is safe to print and audit; the message
    never contains content from the file.
    """

    MESSAGES = {
        "not_found": "plan not found",
        "invalid_json": "plan file is not valid JSON",
        "invalid_schema": "plan file does not match the plan schema",
        "unsupported_contract_version": "plan contract version is not supported; replan required",
        "replan_required": "plan was created before destination binding; replan required",
    }

    def __init__(self, category: str):
        super().__init__(self.MESSAGES[category])
        self.category = category


@dataclass(frozen=True)
class PlanArtifact:
    plan_id: str
    action: str
    secret_name: str | None
    env: str
    status: str
    secret_value_included: bool
    source_env: str | None = None
    target_env: str | None = None
    target: dict[str, Any] | None = None
    secret_mappings: list[dict[str, str]] | None = None
    generator: dict[str, Any] | None = None
    create_mode: str | None = None
    rotation_mode: str | None = None
    source_file: str | None = None
    value_source: str | None = None
    contract_version: int | None = None
    vault_destination: dict[str, str] | None = None
    approval_digest: str | None = None
    # The parsed file content this artifact was loaded from. Status writes
    # replace only the status in this content, never reload the file.
    loaded_content: dict[str, Any] | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("loaded_content")
        return {key: value for key, value in data.items() if value is not None}


def plan_dir() -> Path:
    return Path(os.environ.get(PLAN_DIR_ENV, DEFAULT_PLAN_DIR))


def build_plan_artifact(recipe, vault_destination: dict[str, str] | None = None) -> PlanArtifact:
    """Build a new plan. Vault actions need the configured vault destination."""
    target = None
    secret_mappings = None
    generator = None
    create_mode = None
    rotation_mode = None
    source_file = None
    source_env = None
    target_env = None
    value_source = None
    github_host = recipe.github_host or DEFAULT_GITHUB_HOST
    if recipe.action in {"github.env_secret.plan", "github.env_secret.write"}:
        target = GitHubEnvSecretTarget(
            owner=recipe.github_owner,
            repo=recipe.github_repo,
            environment=recipe.github_environment,
            secret_name=recipe.github_secret_name,
            host=github_host,
        ).describe_write_plan()
    elif recipe.action in {"github.env_secret.sync_many", "github.env_secret.verify_many"}:
        target = {
            "target_host": github_host,
            "target_owner": recipe.github_owner,
            "target_repo": recipe.github_repo,
            "target_environment": recipe.github_environment,
            "api_call_made": False,
            "secret_value_printed": False,
        }
        secret_mappings = recipe.secrets or []
    elif recipe.action == "vault.secret.generate":
        generator = recipe.generator or {}
        create_mode = recipe.create_mode or "create_only"
    elif recipe.action == "vault.secret.rotate":
        generator = recipe.generator or {}
        rotation_mode = recipe.rotation_mode or "replace_existing"
    elif recipe.action == "vault.secret.import_file":
        create_mode = recipe.create_mode or "create_only"
        source_file = recipe.source_file
        if not source_file:
            raise ValueError("missing source file for vault.secret.import_file")
        secret_mappings = [
            {"name": entry.name} for entry in parse_import_source_file(Path(source_file))
        ]
    elif recipe.action == "vault.secret.copy_many":
        source_env = recipe.source_env or recipe.env
        target_env = recipe.target_env
        create_mode = recipe.create_mode or "create_only"
        secret_mappings = recipe.secrets or []
    elif recipe.action == "vault.secret.diff_envs":
        source_env = recipe.source_env or recipe.env
        target_env = recipe.target_env
        secret_mappings = recipe.secrets or []
    elif recipe.action == "vault.secret.add":
        create_mode = recipe.create_mode or "create_only"
        value_source = recipe.value_source or HUMAN_PROMPT_SOURCE

    if recipe.action in VAULT_ACTIONS:
        if vault_destination is None:
            raise ValueError(f"{recipe.action} plan requires a vault destination")
    else:
        vault_destination = None

    artifact = PlanArtifact(
        plan_id=uuid.uuid4().hex,
        action=recipe.action,
        secret_name=recipe.secret_name,
        env=recipe.env,
        status="planned",
        source_env=source_env,
        target_env=target_env,
        secret_value_included=False,
        target=target,
        secret_mappings=secret_mappings,
        generator=generator,
        create_mode=create_mode,
        rotation_mode=rotation_mode,
        source_file=source_file,
        value_source=value_source,
        contract_version=CONTRACT_VERSION,
        vault_destination=dict(vault_destination) if vault_destination else None,
    )
    # Refuse a plan that could not be loaded again before anything is saved.
    try:
        validate_plan_content(artifact.to_dict(), artifact.plan_id)
    except PlanFileError as error:
        raise ValueError(f"plan does not match the plan schema: {error.category}") from error
    return artifact


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _reject_float(_text: str) -> Any:
    raise ValueError("non-integer number")


def parse_plan_json(text: str) -> Any:
    """Parse plan JSON, rejecting duplicate keys, NaN/Infinity, and floats."""
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
        parse_float=_reject_float,
    )


def _is_type(value: Any, expected: type) -> bool:
    # bool is a subclass of int in Python but a different JSON type.
    if expected is int:
        return type(value) is int
    return isinstance(value, expected)


def _check_fields(data: dict[str, Any], types: dict[str, type], *, required: set[str]) -> None:
    if not set(data) <= set(types) or not required <= set(data):
        raise PlanFileError("invalid_schema")
    for key, value in data.items():
        if not _is_type(value, types[key]):
            raise PlanFileError("invalid_schema")


def validate_plan_content(data: Any, expected_plan_id: str | None = None) -> None:
    """Check a contract version 1 plan against the strict schema.

    Raises PlanFileError. Validators never change a value here: a value that
    would need changing is rejected.
    """
    if not isinstance(data, dict):
        raise PlanFileError("invalid_schema")
    if "contract_version" not in data:
        raise PlanFileError("replan_required")
    if not _is_type(data["contract_version"], int) or data["contract_version"] != CONTRACT_VERSION:
        raise PlanFileError("unsupported_contract_version")

    action = data.get("action")
    if not isinstance(action, str) or action not in ACTION_SCHEMAS:
        raise PlanFileError("invalid_schema")
    action_required, action_optional, mapping_shape = ACTION_SCHEMAS[action]
    required = set(REQUIRED_FIELD_TYPES) | action_required
    if action in VAULT_ACTIONS:
        required.add("vault_destination")
    allowed = required | action_optional | {"approval_digest"}
    types = dict(REQUIRED_FIELD_TYPES)
    types.update({key: OPTIONAL_FIELD_TYPES[key] for key in allowed - set(REQUIRED_FIELD_TYPES)})
    _check_fields(data, types, required=required)

    if expected_plan_id is not None and data["plan_id"] != expected_plan_id:
        raise PlanFileError("invalid_schema")
    if data["status"] not in PLAN_STATUSES or data["secret_value_included"] is not False:
        raise PlanFileError("invalid_schema")
    if "approval_digest" in data and not data["approval_digest"].startswith(DIGEST_PREFIX):
        raise PlanFileError("invalid_schema")

    try:
        if "vault_destination" in data:
            destination = data["vault_destination"]
            _check_fields(
                destination,
                {key: str for key in VAULT_DESTINATION_FIELDS},
                required=set(VAULT_DESTINATION_FIELDS),
            )
            # The same check the adapter applies: supported backend,
            # canonical URL without credentials, absolute path.
            try:
                InfisicalConnection.from_destination(destination)
            except VaultError as error:
                raise PlanFileError("invalid_schema") from error
        if "target" in data:
            target_types = dict(GITHUB_TARGET_FIELD_TYPES)
            target_required = set(GITHUB_TARGET_FIELD_TYPES)
            if action in GITHUB_SINGLE_SECRET_ACTIONS:
                target_types["target_secret_name"] = str
                target_required.add("target_secret_name")
            target = data["target"]
            _check_fields(target, target_types, required=target_required)
            validate_github_host(target["target_host"])
            validate_github_target_metadata(
                owner=target["target_owner"],
                repo=target["target_repo"],
                environment=target["target_environment"],
            )
            if "target_secret_name" in target:
                validate_secret_name(target["target_secret_name"])
        if "generator" in data:
            _check_fields(
                data["generator"], GENERATOR_FIELD_TYPES, required=GENERATOR_REQUIRED_FIELDS
            )
        if "secret_mappings" in data:
            mappings = data["secret_mappings"]
            if not mappings:
                raise PlanFileError("invalid_schema")
            for mapping in mappings:
                if not isinstance(mapping, dict) or set(mapping) != MAPPING_FIELDS[mapping_shape]:
                    raise PlanFileError("invalid_schema")
                for name in mapping.values():
                    if not isinstance(name, str):
                        raise PlanFileError("invalid_schema")
                    validate_secret_name(name)
        if "secret_name" in data:
            validate_secret_name(data["secret_name"])
        if action in HUMAN_ENTRY_ACTIONS and (
            data["create_mode"] != "create_only" or data["value_source"] != HUMAN_PROMPT_SOURCE
        ):
            raise PlanFileError("invalid_schema")
    except ValueError as error:
        raise PlanFileError("invalid_schema") from error


def plan_digest(data: dict[str, Any]) -> str:
    """Digest of the operation content of a parsed plan.

    Covers every field except `status` and `approval_digest`. The canonical
    form is fixed by DIGEST_PREFIX; changing it needs a new prefix.
    """
    content = {key: value for key, value in data.items() if key not in DIGEST_EXCLUDED_FIELDS}
    canonical = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return DIGEST_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _artifact_from_content(data: dict[str, Any]) -> PlanArtifact:
    # Values are used as parsed: the schema check already fixed their types.
    return PlanArtifact(
        plan_id=data["plan_id"],
        action=data["action"],
        secret_name=data.get("secret_name"),
        env=data["env"],
        status=data["status"],
        secret_value_included=data["secret_value_included"],
        source_env=data.get("source_env"),
        target_env=data.get("target_env"),
        target=data.get("target"),
        secret_mappings=data.get("secret_mappings"),
        generator=data.get("generator"),
        create_mode=data.get("create_mode"),
        rotation_mode=data.get("rotation_mode"),
        source_file=data.get("source_file"),
        value_source=data.get("value_source"),
        contract_version=data["contract_version"],
        vault_destination=data.get("vault_destination"),
        approval_digest=data.get("approval_digest"),
        loaded_content=data,
    )


def _plan_path(plan_id: str) -> Path:
    return plan_dir() / f"{plan_id}.json"


def _read_plan_json(plan_id: str) -> Any:
    try:
        raw = _plan_path(plan_id).read_bytes()
    except FileNotFoundError as error:
        raise PlanFileError("not_found") from error
    try:
        return parse_plan_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise PlanFileError("invalid_json") from error


def load_plan(plan_id: str) -> PlanArtifact:
    """Load a contract version 1 plan, reading the file once.

    Raises PlanFileError for anything that is not a valid, supported plan.
    """
    data = _read_plan_json(plan_id)
    validate_plan_content(data, plan_id)
    return _artifact_from_content(data)


def load_legacy_plan_for_display(plan_id: str) -> PlanArtifact:
    """Load a plan created before destination binding, for show-plan only.

    The result must never be approved or executed.
    """
    data = _read_plan_json(plan_id)
    if not isinstance(data, dict) or "contract_version" in data:
        raise PlanFileError("invalid_schema")
    try:
        secret_name, target, secret_mappings = validate_plan_secret_fields(
            secret_name=data.get("secret_name"),
            target=data.get("target"),
            secret_mappings=data.get("secret_mappings"),
        )
        return PlanArtifact(
            plan_id=str(data["plan_id"]),
            action=str(data["action"]),
            secret_name=secret_name,
            env=str(data["env"]),
            status=str(data["status"]),
            secret_value_included=bool(data["secret_value_included"]),
            source_env=data.get("source_env"),
            target_env=data.get("target_env") or data.get("target_environment"),
            target=target,
            secret_mappings=secret_mappings,
            generator=data.get("generator"),
            create_mode=data.get("create_mode"),
            rotation_mode=data.get("rotation_mode"),
            source_file=data.get("source_file"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PlanFileError("invalid_schema") from error


def _write_plan_content(plan_id: str, data: dict[str, Any]) -> Path:
    """Replace the plan file atomically: readers see the old or the new file."""
    directory = plan_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = _plan_path(plan_id)
    umask = os.umask(0)
    os.umask(umask)
    file_descriptor, temp_name = tempfile.mkstemp(dir=directory, prefix=".plan-", suffix=".tmp")
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o666 & ~umask)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return path


def save_plan_artifact(artifact: PlanArtifact) -> Path:
    return save_plan(artifact)


def save_plan(plan: PlanArtifact) -> Path:
    """Save a new plan. The content must pass the strict schema first."""
    data = plan.to_dict()
    try:
        validate_plan_content(data, plan.plan_id)
    except PlanFileError as error:
        raise ValueError(f"plan does not match the plan schema: {error.category}") from error
    return _write_plan_content(plan.plan_id, data)


def set_plan_status(
    plan: PlanArtifact, status: str, *, approval_digest: str | None = None
) -> PlanArtifact:
    """Write a new status (and optionally the approval digest) into the
    content this plan was loaded from. The file is not read again."""
    if status not in PLAN_STATUSES:
        raise ValueError(f"unsupported plan status: {status}")
    if plan.loaded_content is None:
        raise ValueError("plan status can only change on a loaded plan")
    data = dict(plan.loaded_content)
    data["status"] = status
    if approval_digest is not None:
        data["approval_digest"] = approval_digest
    validate_plan_content(data, plan.plan_id)
    _write_plan_content(plan.plan_id, data)
    return _artifact_from_content(data)


@contextlib.contextmanager
def plan_lock(plan_id: str) -> Iterator[None]:
    """Hold the exclusive completion lock of one plan, or raise PlanBusyError.

    The lock is an advisory flock on a file beside the plan. It is released
    when the block ends or the process dies; it is never waited for.
    """
    if not PLAN_ID_PATTERN.fullmatch(plan_id):
        raise PlanFileError("not_found")
    directory = plan_dir()
    directory.mkdir(parents=True, exist_ok=True)
    file_descriptor = os.open(directory / f".{plan_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PlanBusyError("plan is in use by another command") from error
        yield
    finally:
        os.close(file_descriptor)
