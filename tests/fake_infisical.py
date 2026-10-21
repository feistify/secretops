"""Machine-token behaviour of `infisical secrets set`, shared by test fakes.

Mirrors Infisical CLI v0.43.99 (packages/cmd/secrets.go and
packages/util/secrets.go) when a token is used:

- `NAME=value` arguments are stored literally; `NAME=@path` is NOT expanded
  (only the user-session branch reads the file), so it stores `@path`
- `--file` is parsed as YAML for `.yaml`/`.yml` and as .env otherwise; the
  .env parser strips surrounding whitespace and quotes
- YAML values must be strings
- file values that are empty or whitespace-only are rejected, and so are
  empty argument values
- `--file` and `NAME=value` arguments are mutually exclusive

PyYAML stands in for the CLI's Go YAML parser here, so this fake cannot
prove YAML 1.2 compatibility; the adapter tests check the file is fully
double-quoted, and the real CLI read-back is the final check.
"""

import yaml


class SetRejected(Exception):
    """The real CLI would exit non-zero without storing anything."""


def _parse_dotenv(content):
    parsed = {}
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "=" not in line:
            raise SetRejected("invalid .env line")
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        parsed[key] = value
    return parsed


def parse_set_args(args):
    """Return ({name: stored_value}, file_path) for `secrets set` args.

    `args` is everything after `set`. Raises SetRejected where the CLI exits
    non-zero before storing.
    """
    positional = []
    file_path = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--env":
            index += 2
            continue
        if arg.startswith("--file="):
            file_path = arg.split("=", 1)[1]
        elif not arg.startswith("--"):
            positional.append(arg)
        index += 1

    if file_path is not None and positional:
        raise SetRejected("--file and arguments are mutually exclusive")

    stored = {}
    for arg in positional:
        if "=" not in arg:
            raise SetRejected("invalid argument format")
        key, value = arg.split("=", 1)
        stored[key] = value

    if file_path is not None:
        with open(file_path, encoding="utf-8") as handle:
            content = handle.read()
        if file_path.endswith((".yaml", ".yml")):
            loaded = yaml.safe_load(content)
            if not isinstance(loaded, dict):
                raise SetRejected("YAML file must be a mapping")
            parsed = {}
            for key, value in loaded.items():
                if not isinstance(value, str):
                    raise SetRejected("YAML secret must be a string")
                parsed[str(key)] = value
        else:
            parsed = _parse_dotenv(content)
        for key, value in parsed.items():
            if not value.strip():
                raise SetRejected("empty value")
            stored[key] = value
        if not stored:
            raise SetRejected("no valid secrets found in the file")

    for key, value in stored.items():
        if not key or not value:
            raise SetRejected("empty key or value")
    return stored, file_path
