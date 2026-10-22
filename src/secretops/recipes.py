import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from secretops.policy import require_allowed
from secretops.validation import (
    validate_github_host,
    validate_github_target_metadata,
    validate_optional_secret_name,
    validate_secret_mappings,
    validate_secret_name,
)


@dataclass(frozen=True)
class Recipe:
    action: str
    secret_name: str | None
    env: str
    source_env: str | None = None
    target_env: str | None = None
    github_host: str | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    github_environment: str | None = None
    github_secret_name: str | None = None
    secrets: list[dict[str, str]] | None = None
    generator: dict[str, Any] | None = None
    create_mode: str = "create_only"
    rotation_mode: str | None = None
    source_file: str | None = None
    value_source: str | None = None

    def require_allowed(self) -> None:
        require_allowed(self.action)


class RecipeError(ValueError):
    """A recipe that cannot be planned; the message names the field."""


GITHUB_TARGET = ("github_owner", "github_repo", "github_environment")
# Per action: required fields, optional fields, and the shape of `secrets`
# entries ("mapping": source/target, "name": name). One of the fields in a
# tuple inside `required` is enough. Actions not listed are left to policy.
RECIPE_FIELDS: dict[str, dict[str, Any]] = {
    "vault.read.check": {"required": ("secret_name", "env"), "optional": ()},
    "vault.secret.add": {"required": ("secret_name", "env"), "optional": ("create_mode", "value_source")},
    "vault.secret.generate": {"required": ("secret_name", "env", "generator"), "optional": ("create_mode",)},
    "vault.secret.rotate": {"required": ("secret_name", "env", "generator"), "optional": ("rotation_mode",)},
    "vault.secret.copy_many": {
        "required": (("source_env", "env"), "target_env", "secrets"),
        "optional": ("create_mode",),
        "secrets": "mapping",
    },
    "vault.secret.diff_envs": {
        "required": (("source_env", "env"), "target_env", "secrets"),
        "optional": (),
        "secrets": "name",
    },
    "vault.secret.import_file": {"required": ("env", "source_file"), "optional": ("create_mode", "secret_name")},
    # GitHub actions read the vault environment `env`, which defaults to
    # github_environment.
    "github.env_secret.plan": {
        "required": ("secret_name", *GITHUB_TARGET, "github_secret_name"),
        "optional": ("env", "github_host"),
    },
    "github.env_secret.write": {
        "required": ("secret_name", *GITHUB_TARGET, "github_secret_name"),
        "optional": ("env", "github_host"),
    },
    "github.env_secret.sync_many": {
        "required": (*GITHUB_TARGET, "secrets"),
        "optional": ("env", "github_host"),
        "secrets": "mapping",
    },
    "github.env_secret.verify_many": {
        "required": (*GITHUB_TARGET, "secrets"),
        "optional": ("github_host", "env"),
        "secrets": "name",
    },
}
# Fields whose value is text. YAML reads 2048 or true as other types, so a
# repository or environment named like that has to be quoted.
TEXT_FIELDS = (
    "secret_name", "env", "source_env", "target_env", "source_file", "create_mode", "rotation_mode",
    "value_source", "github_host", "github_owner", "github_repo", "github_environment", "github_secret_name",
)
FIXED_VALUES = {"create_mode": "create_only", "rotation_mode": "replace_existing", "value_source": "human_prompt"}


def _field_names(fields) -> list[str]:
    names: list[str] = []
    for field in fields:
        names.extend(field if isinstance(field, tuple) else (field,))
    return names


def check_recipe_fields(data: dict[str, Any]) -> None:
    """Refuse unknown, missing or unsupported fields before anything is planned."""
    action = data["action"]
    spec = RECIPE_FIELDS.get(action)
    if spec is None:
        return
    for field in TEXT_FIELDS:
        if field in data and data[field] is not None and not isinstance(data[field], str):
            raise RecipeError(f"{field} must be text; put quotes around it, for example '2048'")
    allowed = ["action", *_field_names(spec["required"]), *_field_names(spec["optional"])]
    for field in data:
        if field not in allowed:
            # A shortened name (github_env) means the field it starts.
            close = [name for name in allowed if name.startswith(str(field))][:1] \
                or difflib.get_close_matches(str(field), allowed, n=1)
            hint = f"; did you mean {close[0]}?" if close else f"; allowed: {', '.join(allowed[1:])}"
            raise RecipeError(f"unknown field {field} for {action}{hint}")
    for field in spec["required"]:
        options = field if isinstance(field, tuple) else (field,)
        if not any(data.get(option) not in (None, "") for option in options):
            raise RecipeError(f"missing field {' or '.join(options)} for {action}")
    for field, value in FIXED_VALUES.items():
        if field in data and data[field] != value:
            raise RecipeError(f"{field} must be {value}")
    if "generator" in data:
        generator = data["generator"]
        if not isinstance(generator, dict) or set(generator) - {"type", "length"}:
            raise RecipeError("generator takes only type and length")
        if generator.get("type") != "password":
            raise RecipeError("generator.type must be password")
        length = generator.get("length", 32)
        if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= 4096:
            raise RecipeError("generator.length must be a whole number from 1 to 4096")
    shape = spec.get("secrets")
    if shape:
        entries = data["secrets"]
        keys = {"mapping": {"source", "target"}, "name": {"name"}}[shape]
        example = "source and target" if shape == "mapping" else "name"
        if not isinstance(entries, list) or not entries:
            raise RecipeError(f"secrets must be a non-empty list of entries with {example}")
        for number, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict) or set(entry) != keys:
                raise RecipeError(f"secrets entry {number} must have exactly {example}")


def _load_secret_mappings(data: dict[str, Any]) -> list[dict[str, str]] | None:
    raw_mappings = data.get("secrets")
    if raw_mappings is None:
        return None
    mappings: list[dict[str, str]] = []
    for mapping in raw_mappings:
        if "name" in mapping:
            mappings.append({"name": validate_secret_name(mapping.get("name"))})
            continue
        mappings.append(
            {
                "source": validate_secret_name(mapping.get("source")),
                "target": validate_secret_name(mapping.get("target")),
            }
        )
    return validate_secret_mappings(mappings)


def _load_value_source(data: dict[str, Any]) -> str | None:
    # Recipes describe where a value will come from, never the value itself.
    if "value_source" not in data:
        return None
    if data.get("action") != "vault.secret.add" or data["value_source"] != "human_prompt":
        raise ValueError("value_source is supported only as human_prompt for vault.secret.add")
    return "human_prompt"


def load_recipe(path: str | Path) -> Recipe:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError as error:
        raise RecipeError(f"cannot read recipe: {error.strerror or error}") from error
    except yaml.YAMLError as error:
        raise RecipeError("recipe is not valid YAML") from error
    if not isinstance(data, dict):
        raise RecipeError("a recipe must be a YAML mapping of fields")
    if not data.get("action"):
        raise RecipeError("missing field action")
    if not isinstance(data["action"], str):
        raise RecipeError("action must be text, for example action: vault.read.check")
    check_recipe_fields(data)
    github_owner = data.get("github_owner")
    github_repo = data.get("github_repo")
    github_environment = data.get("github_environment")
    if github_owner is not None or github_repo is not None or github_environment is not None:
        github_owner, github_repo, github_environment = validate_github_target_metadata(
            owner=github_owner,
            repo=github_repo,
            environment=github_environment,
        )
    github_host = data.get("github_host")
    if github_host is not None:
        github_host = validate_github_host(github_host)
    return Recipe(
        action=str(data["action"]),
        secret_name=validate_optional_secret_name(data.get("secret_name")),
        env=str(data.get("env", data.get("source_env", github_environment or ""))),
        source_env=str(data["source_env"]) if "source_env" in data else None,
        target_env=str(data["target_env"]) if "target_env" in data else None,
        github_host=github_host,
        github_owner=github_owner,
        github_repo=github_repo,
        github_environment=github_environment,
        github_secret_name=validate_optional_secret_name(data.get("github_secret_name")),
        secrets=_load_secret_mappings(data),
        generator=data.get("generator"),
        create_mode=str(data.get("create_mode", "create_only")),
        rotation_mode=data.get("rotation_mode"),
        source_file=data.get("source_file"),
        value_source=_load_value_source(data),
    )


