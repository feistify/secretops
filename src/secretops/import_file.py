import stat
from dataclasses import dataclass
from pathlib import Path

from secretops.validation import validate_secret_name


class ImportFileError(ValueError):
    """Safe import-file validation error whose message never includes values."""


@dataclass(frozen=True)
class DotenvEntry:
    name: str
    value: str


def source_file_permissions_restricted(path: Path) -> bool:
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode & 0o077 == 0


def parse_import_source_file(path: Path) -> list[DotenvEntry]:
    if not path.exists():
        raise ImportFileError("source file exists: False")
    if not path.is_file():
        raise ImportFileError("source file is file: False")
    if not source_file_permissions_restricted(path):
        raise ImportFileError("source file permissions restricted: False")
    return parse_dotenv_file(path)


def parse_dotenv_file(path: Path) -> list[DotenvEntry]:
    entries: list[DotenvEntry] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ImportFileError("source file is not valid UTF-8 dotenv") from error

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise ImportFileError(f"invalid dotenv line: {line_number}")
        raw_name, raw_value = line.split("=", 1)
        raw_name = raw_name.strip()
        try:
            name = validate_secret_name(raw_name)
        except ValueError as error:
            raise ImportFileError(
                f"invalid dotenv key name at line {line_number}"
            ) from error
        if name in seen:
            raise ImportFileError(f"duplicate dotenv key: {name}")
        seen.add(name)
        entries.append(DotenvEntry(name=name, value=_decode_dotenv_value(raw_value)))
    if not entries:
        raise ImportFileError("dotenv file contains no secret names")
    return entries


def _decode_dotenv_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def planned_import_names(secret_mappings: list[dict[str, str]] | None, secret_name: str | None) -> list[str]:
    if secret_mappings:
        return [mapping["name"] for mapping in secret_mappings]
    if secret_name:
        return [secret_name]
    return []
