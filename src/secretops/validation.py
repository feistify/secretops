import re
from typing import Any

SECRET_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
GITHUB_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
# Repos may start with "." or "_" (e.g. the org-level ".github" repo) but must
# never start with "-" so values cannot be parsed as provider CLI flags.
GITHUB_REPO_PATTERN = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")
GITHUB_RESERVED_REPO_NAMES = {".", ".."}
GITHUB_ENVIRONMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
# Lowercase DNS host name without port. The host is passed to gh as part of
# `--repo HOST/OWNER/REPO`, so it is never normalised, only accepted or rejected.
GITHUB_HOST_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$"
)
DEFAULT_GITHUB_HOST = "github.com"

# Names the Infisical CLI drops from `infisical run` (packages/cmd/run.go),
# plus variables secretops passes to the provider process. A vault secret with
# one of these names would be shadowed by the broker's own value.
RESERVED_SECRET_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "PS1",
        "PS2",
        "PWD",
        "EDITOR",
        "XAUTHORITY",
        "USER",
        "TERM",
        "TERMINFO",
        "SHELL",
        "MAIL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
RESERVED_SECRET_NAME_PREFIXES = ("XDG_", "LC_", "INFISICAL_", "SECRETOPS_", "PYTHON", "LD_")


def validate_secret_name(name: Any) -> str:
    value = str(name) if name is not None else ""
    if not value or SECRET_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid secret name")
    if value in RESERVED_SECRET_NAMES or value.startswith(RESERVED_SECRET_NAME_PREFIXES):
        raise ValueError(f"reserved secret name: {value}")
    return value


def _validate_github_target_value(value: Any, pattern: re.Pattern[str]) -> str:
    text = str(value) if value is not None else ""
    if not text or pattern.fullmatch(text) is None:
        raise ValueError("invalid GitHub target metadata")
    return text


def validate_github_target_metadata(
    *,
    owner: Any,
    repo: Any,
    environment: Any,
) -> tuple[str, str, str]:
    repo_value = _validate_github_target_value(repo, GITHUB_REPO_PATTERN)
    if repo_value in GITHUB_RESERVED_REPO_NAMES:
        raise ValueError("invalid GitHub target metadata")
    return (
        _validate_github_target_value(owner, GITHUB_OWNER_PATTERN),
        repo_value,
        _validate_github_target_value(environment, GITHUB_ENVIRONMENT_PATTERN),
    )


def validate_github_host(host: Any) -> str:
    if not isinstance(host, str) or GITHUB_HOST_PATTERN.fullmatch(host) is None:
        raise ValueError("invalid GitHub host")
    return host


def validate_optional_secret_name(name: Any) -> str | None:
    if name is None:
        return None
    return validate_secret_name(name)


def validate_secret_mappings(mappings: Any) -> list[dict[str, str]] | None:
    if mappings is None:
        return None

    validated: list[dict[str, str]] = []
    for mapping in mappings:
        if "name" in mapping:
            validated.append({"name": validate_secret_name(mapping.get("name"))})
            continue
        validated.append(
            {
                "source": validate_secret_name(mapping.get("source")),
                "target": validate_secret_name(mapping.get("target")),
            }
        )
    return validated


def validate_plan_secret_fields(
    *,
    secret_name: Any = None,
    target: dict[str, Any] | None = None,
    secret_mappings: Any = None,
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, str]] | None]:
    validated_secret_name = validate_optional_secret_name(secret_name)
    validated_target = dict(target) if target is not None else None
    if validated_target is not None:
        if {
            "target_owner",
            "target_repo",
            "target_environment",
        }.issubset(validated_target):
            (
                validated_target["target_owner"],
                validated_target["target_repo"],
                validated_target["target_environment"],
            ) = validate_github_target_metadata(
                owner=validated_target["target_owner"],
                repo=validated_target["target_repo"],
                environment=validated_target["target_environment"],
            )
        if "target_host" in validated_target:
            validated_target["target_host"] = validate_github_host(validated_target["target_host"])
        if "target_secret_name" in validated_target:
            validated_target["target_secret_name"] = validate_secret_name(
                validated_target["target_secret_name"]
            )
    return (
        validated_secret_name,
        validated_target,
        validate_secret_mappings(secret_mappings),
    )
