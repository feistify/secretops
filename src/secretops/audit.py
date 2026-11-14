import json
import os
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_AUDIT_PATH = Path(".audit/secretops.jsonl")
AUDIT_PATH_ENV = "SECRETOPS_AUDIT_PATH"
# Fields every event owns; `details` must not replace them.
CORE_FIELDS = frozenset(
    {"timestamp", "action", "secret_name", "env", "status", "secret_value_logged", "plan_id"}
)


def audit_path() -> Path:
    return Path(os.environ.get(AUDIT_PATH_ENV, DEFAULT_AUDIT_PATH))


def write_audit_event(
    *,
    action: str,
    secret_name: str,
    env: str,
    status: str,
    plan_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append one audit event. `details` may carry secret names and metadata
    only, never values.

    Raises ValueError, before anything is written, when `details` names a
    core field: that is a programming error, not a runtime condition.
    """
    collisions = sorted(CORE_FIELDS.intersection(details or {}))
    if collisions:
        raise ValueError("audit details must not replace core fields: " + ", ".join(collisions))
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "secret_name": secret_name,
        "env": env,
        "status": status,
        "secret_value_logged": False,
    }
    if plan_id is not None:
        event["plan_id"] = plan_id
    if details:
        event.update(details)

    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(event, separators=(",", ":")) + "\n")
