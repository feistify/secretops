import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from secretops.validation import (
    VAULT_ENVIRONMENT_RULE,
    is_github_environment_name,
    is_vault_environment_name,
)

POLICY_PATH_ENV = "SECRETOPS_POLICY_PATH"
DEFAULT_POLICY_RESOURCE_PACKAGE = "secretops.policies"
DEFAULT_POLICY_RESOURCE_NAME = "default.yaml"
# Environment names always treated as production, whatever the vault product
# calls them. `production_environments` in the policy adds further names; it
# cannot remove these. Names are compared without regard to case.
DEFAULT_PRODUCTION_ENVIRONMENTS = ["production", "prod"]
# How long an approval made with approve-plan stays executable.
DEFAULT_APPROVAL_VALID_HOURS = 24
APPROVAL_VALID_HOURS_RANGE = (1, 168)


@dataclass(frozen=True)
class ActionPolicy:
    requires_approval: bool = True
    allowed_environments: list[str] = field(default_factory=list)
    production_allowed: bool = False


@dataclass(frozen=True)
class Policy:
    allowed_actions: list[str]
    denied_actions: list[str]
    actions: dict[str, ActionPolicy]
    approval_valid_hours: int = DEFAULT_APPROVAL_VALID_HOURS
    production_environments: list[str] = field(
        default_factory=lambda: list(DEFAULT_PRODUCTION_ENVIRONMENTS)
    )


POLICY_KEYS = (
    "allowed_actions",
    "denied_actions",
    "actions",
    "approval_valid_hours",
    "production_environments",
)
ACTION_POLICY_KEYS = ("requires_approval", "allowed_environments", "production_allowed")
DOC_HINT = "see docs/CONFIGURATION.md#policy-file"
LIST_KEYS = {
    "allowed_actions", "denied_actions", "allowed_environments", "production_environments"
}
BOOLEAN_KEYS = {"requires_approval", "production_allowed"}
# Every action name the code checks against the policy. A test keeps this
# equal to the plan actions plus the planning check.
KNOWN_ACTIONS = frozenset(
    {
        "vault.read.plan",
        "vault.read.check",
        "vault.secret.generate",
        "vault.secret.rotate",
        "vault.secret.import_file",
        "vault.secret.copy_many",
        "vault.secret.diff_envs",
        "vault.secret.add",
        "github.env_secret.plan",
        "github.env_secret.write",
        "github.env_secret.sync_many",
        "github.env_secret.verify_many",
    }
)
# Names that exist only to be denied: no command performs them.
DENY_ONLY_ACTIONS = frozenset(
    {"secret.reveal", "secret.export_all", "secret.print_value", "production.execute_without_approval"}
)
ACTION_NAMES_HINT = "check its spelling against the known actions"


class PolicyError(Exception):
    """The policy file cannot be used. The message names the file, a key
    and its location at most, never other file content."""


def _key_name(key) -> str:
    text = repr(str(key))
    return text if len(text) <= 80 else text[:77] + "..."


def _fail(source: str, problem: str, fix: str) -> None:
    raise PolicyError(f"policy file {source}: {problem}; {fix}; {DOC_HINT}")


def _check_mapping(mapping, allowed: tuple[str, ...], location: str, source: str) -> None:
    """Keys must be known; list and boolean values must have their type."""
    if not isinstance(mapping, dict):
        _fail(source, f"{location} must be a mapping of keys to values", "correct its layout")
    for key, value in mapping.items():
        if key not in allowed:
            _fail(
                source,
                f"unknown key {_key_name(key)} in {location} (allowed: {', '.join(allowed)})",
                "correct or remove it",
            )
        # A quoted "false" or a plain string must not turn into True or
        # into a list of characters.
        if key in BOOLEAN_KEYS and not isinstance(value, bool):
            _fail(source, f"{key} in {location} must be true or false", "correct its value")
        if key in LIST_KEYS and not (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            _fail(source, f"{key} in {location} must be a list of names", "correct its value")
        if key == "production_environments" and not value:
            # Kept strict: an empty list says nothing; leave the key out.
            _fail(
                source,
                f"{key} in {location} must name at least one environment",
                "list additional production names or remove the key",
            )
        if key == "approval_valid_hours":
            low, high = APPROVAL_VALID_HOURS_RANGE
            # bool is an int in Python; `true` must not mean one hour.
            if type(value) is not int or not low <= value <= high:
                _fail(
                    source,
                    f"{key} in {location} must be a whole number of hours from {low} to {high}",
                    "correct its value",
                )


GITHUB_ENVIRONMENT_RULE = "a letter or digit first, then letters, digits, spaces, '.', '-' or '_'"


def _check_environment_names(names, github: bool, key: str, location: str, source: str) -> None:
    # The message names the list and the position, never the name itself.
    check = is_github_environment_name if github else is_vault_environment_name
    rule = GITHUB_ENVIRONMENT_RULE if github else VAULT_ENVIRONMENT_RULE
    for number, name in enumerate(names, start=1):
        if not check(name):
            _fail(
                source,
                f"entry {number} of {key} in {location} is not a valid environment name",
                f"use {rule}",
            )


def validate_policy_data(data, source: str) -> None:
    """Refuse unknown keys and wrongly typed values at the top level and in
    each `actions` entry.

    Action names in `allowed_actions`, `denied_actions` and under `actions`
    must be known: a misspelled name would otherwise never apply. Names
    that exist only to be denied are accepted in the two lists.

    Environment names must be vault environment names. The policy does not
    separate vault from GitHub environments: lists that can also name a
    GitHub environment (`production_environments`, and `allowed_environments`
    of `github.` actions) accept GitHub environment names, which may contain
    spaces.
    """
    _check_mapping(data, POLICY_KEYS, "the top level", source)
    if "production_environments" in data:
        _check_environment_names(
            data["production_environments"], True,
            "production_environments", "the top level", source,
        )
    actions = data.get("actions")
    if actions is not None:
        if not isinstance(actions, dict):
            _fail(source, "actions must be a mapping of action names to settings", "correct its layout")
        for action, metadata in actions.items():
            if metadata is not None:
                location = f"actions entry {_key_name(action)}"
                _check_mapping(metadata, ACTION_POLICY_KEYS, location, source)
                _check_environment_names(
                    metadata.get("allowed_environments", []), str(action).startswith("github."),
                    "allowed_environments", location, source,
                )
    _check_action_names(data, source)


def _check_action_names(data, source: str) -> None:
    """A misspelled action name would never allow, deny or configure
    anything. Checked after the layout. List entries are named by position,
    never by their text; names under `actions` are keys and are named."""
    for key in ("allowed_actions", "denied_actions"):
        for number, name in enumerate(data.get(key) or [], start=1):
            if name not in KNOWN_ACTIONS | DENY_ONLY_ACTIONS:
                _fail(source, f"entry {number} of {key} in the top level is not a known action", ACTION_NAMES_HINT)
    for action in data.get("actions") or {}:
        if action not in KNOWN_ACTIONS:
            _fail(source, f"unknown action {_key_name(action)} in actions", ACTION_NAMES_HINT)


class _DuplicateKey(Exception):
    def __init__(self, key, line: int):
        super().__init__()
        self.key = key
        self.line = line


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a key repeated in one mapping, instead of
    keeping the last one."""


def _construct_unique_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            repeated = key in seen
        except TypeError:
            continue  # an unhashable key fails in the default constructor
        if repeated:
            raise _DuplicateKey(key, key_node.start_mark.line + 1)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _load_action_policies(data: dict) -> dict[str, ActionPolicy]:
    policies = {}
    for action, metadata in (data.get("actions") or {}).items():
        metadata = metadata or {}
        policies[str(action)] = ActionPolicy(
            requires_approval=bool(metadata.get("requires_approval", True)),
            allowed_environments=[
                str(environment)
                for environment in metadata.get(
                    "allowed_environments", []
                )
            ],
            production_allowed=bool(metadata.get("production_allowed", False)),
        )
    return policies


def _load_default_policy_text() -> str:
    return (
        resources.files(DEFAULT_POLICY_RESOURCE_PACKAGE)
        .joinpath(DEFAULT_POLICY_RESOURCE_NAME)
        .read_text(encoding="utf-8")
    )


def _load_policy_text(path: str | Path | None = None) -> tuple[str, str]:
    """Return the policy text and a name for it in messages."""
    explicit_path = path or os.environ.get(POLICY_PATH_ENV)
    if explicit_path:
        try:
            return Path(explicit_path).read_text(encoding="utf-8"), str(explicit_path)
        except (OSError, UnicodeDecodeError) as error:
            raise PolicyError(
                f"policy file {explicit_path} is missing or not readable by this account; {DOC_HINT}"
            ) from error
    return _load_default_policy_text(), "(packaged default)"


def load_policy(path: str | Path | None = None) -> Policy:
    """Load and strictly validate the policy. Raises PolicyError."""
    text, source = _load_policy_text(path)
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
    except _DuplicateKey as duplicate:
        _fail(
            source,
            f"duplicate key {_key_name(duplicate.key)} at line {duplicate.line}",
            "keep only one of them",
        )
    except yaml.YAMLError as error:
        # The parser message can quote file content; only the line is kept.
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise PolicyError(f"policy file {source} is not valid YAML{where}") from None
    data = {} if data is None else data
    validate_policy_data(data, source)
    return Policy(
        allowed_actions=list(data.get("allowed_actions", [])),
        denied_actions=list(data.get("denied_actions", [])),
        actions=_load_action_policies(data),
        approval_valid_hours=data.get("approval_valid_hours", DEFAULT_APPROVAL_VALID_HOURS),
        production_environments=list(
            dict.fromkeys(
                DEFAULT_PRODUCTION_ENVIRONMENTS + data.get("production_environments", [])
            )
        ),
    )


def is_allowed(action: str, policy: Policy | None = None) -> bool:
    policy = policy or load_policy()
    return action in policy.allowed_actions and action not in policy.denied_actions


def require_allowed(action: str, policy: Policy | None = None) -> None:
    if not is_allowed(action, policy):
        raise PermissionError(f"Action denied by policy: {action}")


def get_action_policy(action: str, policy: Policy | None = None) -> ActionPolicy:
    policy = policy or load_policy()
    return policy.actions.get(action, ActionPolicy())


def requires_approval(action: str, policy: Policy | None = None) -> bool:
    return get_action_policy(action, policy).requires_approval


def is_production_allowed(action: str, policy: Policy | None = None) -> bool:
    return get_action_policy(action, policy).production_allowed


def is_production_environment(env: str, policy: Policy | None = None) -> bool:
    policy = policy or load_policy()
    # Case-insensitive, so `Prod` listed in allowed_environments cannot
    # sidestep the block.
    return env.casefold() in {name.casefold() for name in policy.production_environments}


def require_environment_allowed(
    action: str,
    env: str,
    policy: Policy | None = None,
) -> None:
    """A production environment (`production`, `prod`, and any name in
    `production_environments`, in any case) needs `production_allowed`
    for the action, whether or not it is also listed in the action's
    `allowed_environments`; every environment must be listed there."""
    policy = policy or load_policy()
    require_allowed(action, policy)
    action_policy = get_action_policy(action, policy)
    if is_production_environment(env, policy) and not action_policy.production_allowed:
        raise PermissionError(
            f"production execution denied by policy: {action} "
            f"(environment {env} is a production environment by policy)"
        )
    if env not in action_policy.allowed_environments:
        raise PermissionError(f"environment denied by policy for {action}: {env}")
